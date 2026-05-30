"""
scrapers/cabinet_le_nail.py — Cabinet Le Nail (châteaux, manoirs, propriétés de prestige)
Méthode : httpx pur (SSR, schema.org). Pas de filtre département côté serveur →
on parcourt le listing national et on post-filtre par préfixe de code postal.
Interface : async def search(criteres: dict) -> list[dict]

Biens de caractère (châteaux, manoirs, longères, maisons de maître) — bonne cible
pour un profil "demeure ancienne". Prix souvent élevés ; certains "Nous consulter".
"""
import asyncio
import re

import httpx

BASE_URL = "https://www.cabinetlenail.com"
LIST_URL = f"{BASE_URL}/fr/chateaux-et-proprietes-a-vendre-france/"
MAX_PAGES = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_ARTICLE_RE = re.compile(r'<article[^>]*annonce_listing.*?</article>', re.S)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}

    results, seen = [], set()
    async with httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True) as client:
        for page in range(1, MAX_PAGES + 1):
            url = LIST_URL if page == 1 else f"{LIST_URL}{page}/"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[CabinetLeNail] page {page}: {e}")
                break
            if r.status_code != 200:
                break

            page_biens = [b for art in _ARTICLE_RE.findall(r.text)
                          if (b := _parse_article(art))]
            # Stop dès qu'une page n'apporte plus de nouvelle annonce.
            new = [b for b in page_biens if b["id_annonce"] not in seen]
            if not new:
                break
            for b in new:
                seen.add(b["id_annonce"])
                if not departements or (b["code_postal"][:2] in departements):
                    results.append(b)

    print(f"[CabinetLeNail] {len(results)} annonce(s) dans les départements ciblés")
    return results


def _parse_article(html: str) -> dict | None:
    try:
        m_url = re.search(r'itemprop=["\']url["\']\s+href=["\']([^"\']+)["\']', html)
        if not m_url:
            return None
        path = m_url.group(1)
        ref = re.search(r'ref-([a-z0-9]+-\d+)', path)
        ad_id = ref.group(1) if ref else path

        title = re.search(r'title=["\']([^"\']+)["\']', html)
        title = title.group(1) if title else ""

        # "château 15 pièces en vente à AUBIGNY SUR NERE (18700) - Plus de details"
        type_bien = (re.match(r'\s*([a-zàâéèêûïôç\-]+)', title.lower()) or [None, ""])[1].strip()
        pieces = re.search(r'(\d+)\s*pi[èe]ce', title)
        cp = re.search(r'\((\d{5})\)', title)
        ville = re.search(r'\b[àa]\s+(.+?)\s*\(\d{5}\)', title)

        # Prix : meta itemprop="price" content="…" (numérique) ou "Nous consulter".
        m_price = re.search(r'itemprop=["\']price["\']\s+content=["\']?(\d[\d ]*)', html)
        prix = float(m_price.group(1).replace(" ", "")) if m_price else None

        img = re.search(r'itemprop=["\']image["\'][^>]*data-src=["\']([^"\']+)', html)
        photos = [BASE_URL + img.group(1)] if img and img.group(1).startswith("/") else (
            [img.group(1)] if img else [])

        return {
            "source": "cabinet_le_nail",
            "url": BASE_URL + path if path.startswith("/") else path,
            "id_annonce": ad_id,
            "titre": title.split(" - ")[0].strip(),
            "type_bien": type_bien or "propriete",
            "ville": ville.group(1).strip().title() if ville else "",
            "code_postal": cp.group(1) if cp else "",
            "departement": cp.group(1)[:2] if cp else "",
            "pieces": int(pieces.group(1)) if pieces else None,
            "prix": prix,
            "photos": photos,
        }
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria
    criteres = load_criteria()
    biens = asyncio.run(search({"departements": criteres.departements}))
    print(f"\nTotal Cabinet Le Nail (départements ciblés) : {len(biens)}")
    for b in biens[:8]:
        print(f"  {b['type_bien']:<10} {b['ville']:<22} {b['code_postal']} — "
              f"{b['prix'] or 'Nous consulter'} — {b['pieces']}p — {b['url'][-40:]}")

"""
scrapers/avendre_alouer.py — À Vendre À Louer (Groupe Ouest-France Multimédia)
Méthode : httpx pur — SSR HTML
URL : /immobilier/vente-maison-{slug}/  →  paginé avec ?p=N
Post-filtre par code postal pour garantir le département cible.
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import parse_int as _re_int
from scrapers._base import parse_str_upper as _re_str

BASE = "https://www.avendre-alouer.fr"

# Slugs département → URL avendre-alouer
DEPT_SLUGS = {
    "72": "sarthe-72",
    "28": "eure-et-loir-28",
    "45": "loiret-45",
    "89": "yonne-89",
    "49": "maine-et-loire-49",
    "37": "indre-et-loire-37",
    "36": "indre-36",
    "18": "cher-18",
    "58": "nievre-58",
    "41": "loir-et-cher-41",
    "53": "mayenne-53",
    "44": "loire-atlantique-44",
    "85": "vendee-85",
    "35": "ille-et-vilaine-35",
    "61": "orne-61",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

MAX_PAGES = 8


# ── Helpers ──────────────────────────────────────────────────────────────

def _re_float(pattern: str, text: str, *, replace_space: bool = True) -> float | None:
    m = re.search(pattern, text.replace("\xa0", " "))
    if not m:
        return None
    try:
        raw = m.group(1).replace(" ", "").replace(",", ".")
        return float(raw)
    except Exception:
        return None


# ── Parseur HTML ──────────────────────────────────────────────────────────

def _parse_page(html: str, dept: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []

    # Le site utilise des <article> ou des <div> pour chaque annonce.
    # On essaie plusieurs sélecteurs par ordre de probabilité.
    cards = (
        soup.select("article.announcement")
        or soup.select("li.announcement")
        or soup.select("div.announcement")
        or soup.select("article[class*='listing']")
        or soup.select("div[class*='listing-item']")
        or soup.select("div[class*='property']")
        or soup.select("article")          # fallback large
    )

    # Si le fallback large retourne trop de trucs, on filtre sur ceux qui ont un prix
    if len(cards) > 100:
        cards = [c for c in cards if "€" in c.get_text()]

    seen_urls: set[str] = set()

    for card in cards:
        try:
            bien = _parse_card(card, dept)
            if bien and bien["url"] not in seen_urls:
                seen_urls.add(bien["url"])
                results.append(bien)
        except Exception:
            continue

    return results


def _parse_card(card, dept: str) -> dict | None:
    # ── URL & ID ──
    link = card.select_one("a[href]")
    if not link:
        return None
    href = link.get("href", "")
    if not href or href == "#":
        return None
    url = href if href.startswith("http") else BASE + href

    id_m = re.search(r"/(\d{5,})", href)
    ad_id = id_m.group(1) if id_m else re.sub(r"[^a-z0-9]", "", href)[-16:]
    if not ad_id:
        return None

    text = card.get_text(" ", strip=True).replace("\xa0", " ")

    # ── Prix ──
    prix = _re_float(r"([\d][\d\s]*\d)\s*€", text)
    if not prix or prix < 10_000:
        return None

    # ── Surface ──
    surface = _re_float(r"(\d+(?:[.,]\d+)?)\s*m²", text)

    # ── Terrain ──
    terrain_m = re.search(r"[Tt]errain\s+([\d\s]+)\s*m²", text)
    terrain = float(terrain_m.group(1).replace(" ", "")) if terrain_m else None

    # ── Pièces ──
    pieces = _re_int(r"(\d+)\s*pièces?", text)
    chambres = _re_int(r"(\d+)\s*ch(?:ambres?)?\.?", text)

    # ── Ville / CP ──
    city_m = re.search(r"([A-ZÀ-Ÿa-zà-ÿ][A-ZÀ-Ÿa-zà-ÿ\s'\-]{1,30})\s*\((\d{5})\)", text)
    ville = city_m.group(1).strip() if city_m else ""
    cp = city_m.group(2) if city_m else ""

    # Post-filtre département
    if cp and not cp.startswith(dept):
        return None
    if not cp and ville:
        # Cas rare : pas de CP — on accepte avec flag incertain
        pass

    # ── Titre ──
    title_el = card.select_one("h2, h3, h4, .title, [class*='title']")
    titre = title_el.get_text(strip=True) if title_el else f"Maison {pieces or ''}p. — {ville}"
    titre = titre[:150]

    # ── Photos ──
    photos: list[str] = []
    for img in card.select("img"):
        for attr in ("src", "data-src", "data-lazy-src", "data-original"):
            src = img.get(attr, "")
            if src and src.startswith("http"):
                photos.append(src)
                break
    photos = list(dict.fromkeys(photos))[:8]

    # ── DPE ──
    dpe = _re_str(r"\bDPE\s*:?\s*([A-G])\b", text)

    # ── Agence ──
    agency_el = card.select_one("[class*='agency'], [class*='agence'], [class*='contact']")
    agence = agency_el.get_text(strip=True)[:80] if agency_el else ""

    return {
        "source": "avendre_alouer",
        "url": url,
        "id_annonce": str(ad_id),
        "titre": titre,
        "type_bien": "maison",
        "description": text[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": agence,
    }


# ── Interface principale ──────────────────────────────────────────────────

async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max    = criteres.get("prix_max", 600_000)
    prix_min    = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 80)

    biens: list[dict] = []

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=20) as client:
        for dept in departements:
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                continue

            dept_biens: list[dict] = []
            seen_ids: set[str] = set()

            for page in range(1, MAX_PAGES + 1):
                url = f"{BASE}/immobilier/vente-maison-{slug}/"
                if page > 1:
                    url += f"?p={page}"

                try:
                    r = await client.get(url)
                    if r.status_code == 404:
                        # Essai URL alternative
                        url2 = f"{BASE}/annonces/vente/maison/{slug}/"
                        if page > 1:
                            url2 += f"?p={page}"
                        r = await client.get(url2)
                    if r.status_code != 200:
                        break
                except Exception as e:
                    print(f"[AVendreALouer] ERR dept={dept} page={page}: {e}")
                    break

                page_biens = _parse_page(r.text, dept)
                if not page_biens:
                    break

                added = 0
                for b in page_biens:
                    if b["id_annonce"] in seen_ids:
                        continue
                    if prix_max and b.get("prix") and b["prix"] > prix_max:
                        continue
                    if prix_min and b.get("prix") and b["prix"] < prix_min:
                        continue
                    if surface_min and b.get("surface") and b["surface"] < surface_min:
                        continue
                    seen_ids.add(b["id_annonce"])
                    dept_biens.append(b)
                    added += 1

                print(f"[AVendreALouer] dept={dept} page={page} → {added} nouveaux")

                # Détection fin de pagination
                if (
                    f"?p={page + 1}" not in r.text
                    and f"page={page + 1}" not in r.text
                    and f">>{page + 1}<" not in r.text
                ):
                    break

                await asyncio.sleep(0.4)

            biens.extend(dept_biens)

    print(f"[AVendreALouer] total: {len(biens)} biens")
    return biens


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria
    criteres = load_criteria()

    async def _test():
        result = await search({
            "departements": [72, 53, 28],
            "prix_max": criteres.prix_max,
            "prix_min": criteres.prix_min,
            "surface_min": criteres.surface_min,
        })
        print(f"\nTotal: {len(result)} annonces")
        for b in result[:5]:
            print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']} ({b['code_postal']})")

    asyncio.run(_test())

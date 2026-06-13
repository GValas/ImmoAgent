"""scrapers/paruvendu.py — Paru Vendu

SSR HTML pages with class="blocAnnonce" listing divs.
URL: /immobilier/vente/maison/{dept-slug}/?p={page}
City/dept extracted from "Ville (dept_code)" pattern in stripped text.
"""

import asyncio
import re

import httpx

BASE = "https://www.paruvendu.fr"

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
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,*/*",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_PRICE_RE = re.compile(r"(\d[\d\s]+\d)\s*&euro;")
_CITY_RE = re.compile(r"([A-ZÀ-Ÿa-zà-ÿ][A-ZÀ-Ÿa-zà-ÿ\s'\-]+)\s*\((\d{2})\)")
_SURFACE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m\s*(?:²|2)", re.IGNORECASE)
_PIECES_RE = re.compile(r"(\d+)\s*pièce", re.IGNORECASE)
_PHOTO_RE = re.compile(r"imageElement\.src\s*=\s*'([^']+)'")


def _parse_blocks(html: str, dept: str) -> list[dict]:
    results = []
    for m in re.finditer(
        r'<div\s[^>]*class="blocAnnonce[^"]*"[^>]*>(.*?)(?=<div\s[^>]*class="blocAnnonce|</main)',
        html,
        re.DOTALL,
    ):
        block = m.group(1)

        href_m = re.search(r'href="(/immobilier/vente/maison/[^"]+)"', block)
        if not href_m:
            continue
        href = href_m.group(1)

        title_m = re.search(r'title="([^"]+)"', block)
        titre = title_m.group(1).strip() if title_m else ""

        surf_m = _SURFACE_RE.search(titre)
        surface = float(surf_m.group(1).replace(",", ".")) if surf_m else None

        pieces_m = _PIECES_RE.search(titre)
        pieces = int(pieces_m.group(1)) if pieces_m else None

        price_m = _PRICE_RE.search(block)
        if not price_m:
            continue
        try:
            prix = int(re.sub(r"\s", "", price_m.group(1)))
        except ValueError:
            continue
        if prix < 5000:
            continue

        stripped = re.sub(r"<[^>]+>", " ", block)
        city_m = _CITY_RE.search(stripped)
        if not city_m:
            continue
        ville = city_m.group(1).strip()
        found_dept = city_m.group(2)
        if found_dept != dept:
            continue

        photo_m = _PHOTO_RE.search(block)
        photo_url = photo_m.group(1) if photo_m else None

        results.append({
            "titre": titre or f"Maison à vendre — {ville}",
            "prix": prix,
            "surface": surface,
            "pieces": pieces,
            "ville": ville,
            "code_postal": "",
            "departement": dept,
            "latitude": None,
            "longitude": None,
            "url": BASE + href,
            "photo_url": photo_url,
            "source": "paruvendu",
            "date_ajout": "",
        })
    return results


async def search(criteres: dict) -> list[dict]:
    departements = [str(d) for d in criteres.get("departements", [])]
    biens: list[dict] = []

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=20) as client:
        for dept in departements:
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                print(f"[ParuVendu] no slug for dept {dept}, skipping")
                continue

            page = 1
            MAX_PAGES = 15
            while page <= MAX_PAGES:
                url = f"{BASE}/immobilier/vente/maison/{slug}/"
                if page > 1:
                    url += f"?p={page}"
                try:
                    r = await client.get(url)
                except Exception as e:
                    print(f"[ParuVendu] ERR {url}: {e}")
                    break

                biens_page = _parse_blocks(r.text, dept)
                if not biens_page:
                    break

                biens.extend(biens_page)
                print(f"[ParuVendu] dept={dept} page={page} → {len(biens_page)} biens")

                if f"?p={page + 1}" not in r.text:
                    break
                page += 1
                await asyncio.sleep(0.5)

    print(f"[ParuVendu] total: {len(biens)} biens")
    return biens


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    async def _test():
        results = await search({"departements": [72, 53, 28]})
        for b in results[:5]:
            print(f"  {b['ville']} ({b['departement']}) — {b['prix']}€ — {b['surface']}m² — {b['url']}")

    asyncio.run(_test())

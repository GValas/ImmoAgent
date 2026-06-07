"""scrapers/iad.py — IAD France (mandataires)

REST JSON API at /api/properties
- location param: dept slug like "sarthe-72"
- Filter house types client-side (propertyType)
- Pagination: page=1..N (30 per page)
"""

import asyncio
import math
import httpx

BASE = "https://www.iadfrance.fr"
API = f"{BASE}/api/properties"

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

_HOUSE_TYPES = {"house", "town-house", "home", "country-house", "village-house", "cottage"}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Referer": "https://www.iadfrance.fr/",
}


def _item_to_bien(item: dict, dept: str) -> dict | None:
    if item.get("propertyType") not in _HOUSE_TYPES:
        return None
    if item.get("transactionType") != "sale":
        return None
    prix = (item.get("price") or {}).get("main")
    if not prix or prix < 5000:
        return None

    loc = item.get("location", {})
    postcode = loc.get("postcode", "")

    surface = None
    for s in item.get("surfaceList", []):
        if s.get("type") == "living-area":
            surface = s.get("value")
            break

    pieces = None
    for r in item.get("rooms", []):
        if r.get("type") == "rooms":
            pieces = r.get("value")
            break

    photos = item.get("photos", [])
    slug_fr = item.get("slugs", {}).get("fr", "")
    url = f"{BASE}/annonces/{slug_fr}" if slug_fr else ""

    return {
        "titre": item.get("title", ""),
        "prix": int(prix),
        "surface": float(surface) if surface is not None else None,
        "pieces": pieces,
        "ville": loc.get("place", ""),
        "code_postal": postcode,
        "departement": postcode[:2] if postcode else dept,
        "latitude": None,
        "longitude": None,
        "url": url,
        "photo_url": photos[0] if photos else None,
        "photos": photos,                 # galerie complète (l'API liste la renvoie)
        "id_annonce": item.get("propertyListingRef"),  # pour l'API détail (DPE via gallery)
        "source": "iad",
        "date_ajout": "",
    }


async def search(criteres: dict) -> list[dict]:
    departements = [str(d) for d in criteres.get("departements", [])]
    biens: list[dict] = []

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=20) as client:
        for dept in departements:
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                print(f"[IAD] no slug for dept {dept}, skipping")
                continue

            page = 1
            total_pages = 1
            while page <= total_pages and page <= 25:
                try:
                    r = await client.get(
                        API,
                        params={"transactionType": "Vente", "location": slug, "page": page},
                    )
                    d = r.json()
                except Exception as e:
                    print(f"[IAD] ERR dept={dept} page={page}: {e}")
                    break

                if page == 1:
                    per_page = d.get("itemsPerPage") or 30
                    total = d.get("totalItems") or 0
                    total_pages = math.ceil(total / per_page) if per_page else 1

                items = d.get("items", [])
                if not items:
                    break

                added = 0
                for item in items:
                    bien = _item_to_bien(item, dept)
                    if bien:
                        biens.append(bien)
                        added += 1

                print(f"[IAD] dept={dept} page={page}/{total_pages} → {added} maisons")
                page += 1
                await asyncio.sleep(0.3)

    print(f"[IAD] total: {len(biens)} biens")
    return biens


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    async def _test():
        results = await search({"departements": [72, 53]})
        for b in results[:5]:
            print(f"  {b['ville']} ({b['code_postal']}) — {b['prix']}€ — {b['surface']}m² — {b['url']}")

    asyncio.run(_test())

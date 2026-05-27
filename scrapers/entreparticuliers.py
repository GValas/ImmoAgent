"""scrapers/entreparticuliers.py — EntreparticulierS (P2P portal)

Scrape via HTML pages that embed a Hydra/API Platform JSON collection
in a <script> tag. No direct API access needed.

URL pattern: /annonces-immobilieres/vente/maison/{dept-slug}?page=N
Data: embedded JSON in <script> → hydra:member[] with full listing data
"""

import asyncio
import json
import re
import httpx

BASE = "https://www.entreparticuliers.com"

# Dept code → URL slug mapping (target depts only)
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


def _extract_hydra_members(html: str) -> list[dict]:
    """Parse the embedded script JSON and return hydra:member list."""
    for script in re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL):
        if "hydra:member" not in script:
            continue
        start = script.find("{")
        if start < 0:
            continue
        try:
            outer = json.loads(script[start:])
        except json.JSONDecodeError:
            continue
        for val in outer.values():
            if not isinstance(val, dict) or "b" not in val:
                continue
            collection = val["b"]
            if not isinstance(collection, dict):
                continue
            members = collection.get("hydra:member", [])
            if not members:
                return []
            result = []
            for m in members:
                item = m.get("0", m) if isinstance(m, dict) else m
                if isinstance(item, dict):
                    result.append(item)
            return result
    return []


def _item_to_bien(item: dict, source_label: str = "entreparticuliers") -> dict | None:
    prix = item.get("prix")
    if not prix or prix < 5000:  # skip rentals (monthly price) and null
        return None
    commune = item.get("commune") or {}
    photos = item.get("photos") or []
    photo_url = photos[0].get("publicUrl") if photos else None
    listing_id = item.get("id", "")
    commune_slug = commune.get("slug", "")
    url = f"{BASE}/annonces-immobilieres/{commune_slug}/{listing_id}" if commune_slug else ""
    return {
        "titre": item.get("titre", ""),
        "prix": prix,
        "surface": item.get("surface"),
        "pieces": item.get("piecesnb"),
        "ville": commune.get("label", ""),
        "code_postal": commune.get("codePostal", ""),
        "departement": (commune.get("codePostal") or "")[:2],
        "latitude": item.get("latitude"),
        "longitude": item.get("longitude"),
        "url": url,
        "photo_url": photo_url,
        "source": source_label,
        "date_ajout": (item.get("date") or "")[:10],
    }


async def search(criteres: dict) -> list[dict]:
    departements = [str(d) for d in criteres.get("departements", [])]
    biens: list[dict] = []

    async with httpx.AsyncClient(
        headers=_HEADERS,
        follow_redirects=True,
        timeout=20,
    ) as client:
        for dept in departements:
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                print(f"[EntreparticulierS] no slug for dept {dept}, skipping")
                continue

            page = 1
            seen_ids: set[int] = set()
            MAX_PAGES = 15
            while page <= MAX_PAGES:
                url = f"{BASE}/annonces-immobilieres/vente/maison/{slug}?page={page}"
                try:
                    r = await client.get(url)
                except Exception as e:
                    print(f"[EntreparticulierS] ERR {url}: {e}")
                    break

                members = _extract_hydra_members(r.text)
                if not members:
                    break

                added = 0
                for item in members:
                    item_id = item.get("id")
                    if item_id in seen_ids:
                        continue  # duplicate (server loops sparse depts)
                    seen_ids.add(item_id)
                    bien = _item_to_bien(item)
                    if bien:
                        biens.append(bien)
                        added += 1

                if added == 0:
                    break  # no new items (all duplicates or all filtered)

                print(f"[EntreparticulierS] dept={dept} page={page} → {added} nouveaux")
                page += 1
                await asyncio.sleep(0.5)

    print(f"[EntreparticulierS] total: {len(biens)} biens")
    return biens


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    async def _test():
        depts = {"departements": [72, 28, 53]}
        results = await search(depts)
        for b in results[:5]:
            print(f"  {b['ville']} ({b['code_postal']}) — {b['prix']}€ — {b['surface']}m² — {b['url']}")

    asyncio.run(_test())

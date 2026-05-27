"""
scrapers/remax.py — RE/MAX France (franchise internationale)
Méthode : API REST POST https://www.remax.fr/api/Listing/PaginatedMultiMatchSearch
Filtres : listingPrice + livingArea. Post-filtrage par zipCode (dept code).
Photos  : CDN https://media.remax.fr/listings/{officeNumber}/{id}/{filename}
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

import httpx


BASE_URL = "https://www.remax.fr"
API_URL = f"{BASE_URL}/api/Listing/PaginatedMultiMatchSearch"
CDN_URL = "https://media.remax.fr"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Referer": f"{BASE_URL}/fr/achat/mandats/maison/r/r/r/t",
}

# Filtres de base : maisons à vendre (listingTypeID=8 = maison, businessTypeID=1 = achat)
BASE_FILTERS = [
    {"field": "businessTypeID", "operationType": "int", "operator": "=", "value": "1", "label": "buy"},
    {"field": "listingTypeID", "operationType": "multiple", "operator": "=", "value": "8"},
    {"field": "listingClassID", "operationType": "int", "operator": "=", "value": "1"},
    {"field": "isSpecialExclusive", "operator": "=", "operationType": "string", "value": "false"},
]

PAGE_SIZE = 20
MAX_PAGES = 15  # 300 résultats max — filtre prix+surface réduit à ~200 annonces France


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 600000)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 80)

    # Récupère toutes les annonces France matching prix+surface, puis post-filtre par dept
    all_items = await _fetch_all(prix_min, prix_max, surface_min)

    results = []
    for item in all_items:
        cp = str(item.get("zipCode", "") or "")
        dept_item = cp[:2] if len(cp) >= 2 else ""
        if departements and dept_item not in departements:
            continue
        bien = _parse_item(item, dept_item or "??")
        if bien:
            results.append(bien)

    # Dédup
    seen = set()
    deduped = []
    for b in results:
        if b["id_annonce"] not in seen:
            seen.add(b["id_annonce"])
            deduped.append(b)

    # Log par département
    by_dept = {}
    for b in deduped:
        by_dept.setdefault(b["departement"], []).append(b)
    for dept, items in sorted(by_dept.items()):
        print(f"[REMAX] Dept {dept}: {len(items)} annonces")

    return deduped


async def _fetch_all(prix_min: int, prix_max: int, surface_min: int) -> list[dict]:
    filters = BASE_FILTERS + [
        {"field": "listingPrice", "operationType": "int", "operator": "<=", "value": str(prix_max)},
        {"field": "livingArea", "operationType": "int", "operator": ">=", "value": str(surface_min)},
    ]
    if prix_min:
        filters.append({"field": "listingPrice", "operationType": "int", "operator": ">=", "value": str(prix_min)})

    all_items = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        for page_num in range(1, MAX_PAGES + 1):
            body = {
                "filters": filters,
                "pageNumber": page_num,
                "pageSize": PAGE_SIZE,
                "sort": ["-PublishDate"],
                "searchValue": "",
            }
            try:
                r = await client.post(API_URL, json=body)
                if r.status_code != 200:
                    break
                data = r.json()
            except Exception:
                break

            items = data.get("results", [])
            if not items:
                break
            all_items.extend(items)

            total = data.get("total", 0)
            if page_num * PAGE_SIZE >= total:
                break

    return all_items


def _parse_item(item: dict, dept: str) -> dict | None:
    try:
        ad_id = str(item.get("id", ""))
        if not ad_id:
            return None

        # URL de l'annonce
        desc_tags = item.get("descriptionTags", "")
        url = f"{BASE_URL}/fr/mandats/{desc_tags}" if desc_tags else f"{BASE_URL}/fr/mandats/{ad_id}"

        # Prix et surface
        prix = item.get("listingPrice")
        surface = item.get("livingArea") or item.get("totalArea")
        terrain = item.get("lotSize")

        # Pièces et chambres
        pieces = item.get("totalRooms")
        chambres = item.get("numberOfBedrooms")

        # DPE : energyEfficiencyLevelID (1=A, 2=B, 3=C, 4=D, 5=E, 6=F, 7=G)
        dpe_map = {1: "A", 2: "B", 3: "C", 4: "D", 5: "E", 6: "F", 7: "G"}
        dpe = dpe_map.get(item.get("energyEfficiencyLevelID"))

        # Localisation
        ville = item.get("localZone", "") or item.get("regionName3", "")
        cp = str(item.get("zipCode", ""))

        # Titre depuis descriptions FR
        titre = ""
        for d in item.get("descriptions", []):
            if d.get("languageCode") == "FR":
                # Extrait texte brut depuis HTML
                raw = re.sub(r"<[^>]+>", " ", d.get("description", "")).strip()
                titre = raw[:150]
                break
        if not titre:
            titre = f"Maison {pieces or ''} pièces {ville}".strip()

        # Description FR
        description = ""
        for d in item.get("descriptions", []):
            if d.get("languageCode") == "FR":
                description = re.sub(r"<[^>]+>", " ", d.get("description", "")).strip()[:1200]
                break

        # Photos : chemins relatifs → URL absolue CDN
        photos = []
        office_num = item.get("officeNumber", "")
        for pic_path in item.get("listingPictures", [])[:10]:
            if pic_path.startswith("http"):
                photos.append(pic_path)
            elif pic_path:
                photos.append(f"{CDN_URL}/{pic_path}")

        return {
            "source": "remax",
            "url": url,
            "id_annonce": ad_id,
            "titre": titre,
            "type_bien": "maison",
            "description": description,
            "departement": dept,
            "ville": str(ville)[:80],
            "code_postal": cp,
            "surface": float(surface) if surface else None,
            "surface_terrain": float(terrain) if terrain else None,
            "pieces": int(pieces) if pieces else None,
            "chambres": int(chambres) if chambres else None,
            "prix": float(prix) if prix else None,
            "photos": photos,
            "dpe": dpe,
            "agence": item.get("officeName", "RE/MAX France"),
        }
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria
    criteres = load_criteria()
    biens = asyncio.run(search({
        "departements": criteres.departements[:3],
        "prix_max": criteres.prix_max,
        "prix_min": criteres.prix_min,
        "surface_min": criteres.surface_min,
    }))
    print(f"\nTotal RE/MAX: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

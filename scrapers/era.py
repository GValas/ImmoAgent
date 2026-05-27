"""
scrapers/era.py — ERA Immobilier (eraimmobilier.com)
Méthode : api_inoff (httpx) — API REST à api.eraimmobilier.com/api/v2
Interface : async def search(criteres: dict) -> list[dict]
Note : filtre par code_postal prefix (ex: "72" pour tout le dept 72) — 10 items/page
"""
import re
import asyncio

import httpx


API_BASE = "https://api.eraimmobilier.com/api/v2/annonces"
SITE_BASE = "https://www.eraimmobilier.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Origin": SITE_BASE,
    "Referer": f"{SITE_BASE}/",
}

# Departments covered (2-digit prefix = all postal codes in that dept)
TARGET_DEPTS = {
    "72", "53", "49", "37", "45", "41", "18", "28", "36", "89", "58"
}


async def search(criteres: dict) -> list[dict]:
    departements = criteres.get("departements", [])
    prix_max = criteres.get("prix_max", 600000)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 80)

    results = []
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
        for dept in departements:
            dept_str = str(dept).zfill(2)
            if dept_str not in TARGET_DEPTS:
                continue
            try:
                biens = await _scrape_dept(client, dept_str, prix_max, prix_min, surface_min)
                results.extend(biens)
                print(f"[ERA] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[ERA] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.5)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens = []
    page = 1
    while True:
        r = await client.get(
            API_BASE,
            params={
                "code_postal": dept,
                "type_bien": "Maison",
                "type_annonce": "Vente",
                "page": page,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        items = data.get("data", [])
        if not items:
            break

        for item in items:
            bien = _parse_item(item, dept)
            if not bien:
                continue
            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p > prix_max:
                continue
            if prix_min and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue
            biens.append(bien)

        meta = data.get("meta", {})
        last_page = meta.get("last_page", 1)
        if page >= last_page or page >= 20:
            break
        page += 1
        await asyncio.sleep(0.3)
    return biens


def _parse_item(item: dict, dept: str) -> dict | None:
    prix = item.get("prix") or 0
    if not prix:
        return None

    surface = item.get("surface_habitable") or None
    surface_terrain = item.get("surface_terrain") or None
    if surface_terrain == 0:
        surface_terrain = None

    era_id = item.get("era_id") or item.get("id")
    listing_id = item.get("id") or era_id   # champ `id` = URL réelle sur le site
    ville_raw = item.get("ville", "").strip()
    cp = item.get("code_postal", "")
    ville = ville_raw.title()

    # Listing URL — format confirmé : /annonces/{id}
    url = f"{SITE_BASE}/annonces/{listing_id}"

    # Photos (field is a list of URL strings)
    photos_raw = item.get("photo") or []
    if isinstance(photos_raw, list):
        photos = [p for p in photos_raw if isinstance(p, str) and p.startswith("http")]
    else:
        photos = []
    photos = photos[:10]

    # Agency
    agence_data = item.get("agence") or {}
    if isinstance(agence_data, dict):
        agence = agence_data.get("enseigne") or agence_data.get("nom") or "ERA Immobilier"
    else:
        agence = "ERA Immobilier"

    # DPE from title
    dpe_m = re.search(r"DPE\s*([A-G])", item.get("libelle", ""), re.IGNORECASE)
    dpe = dpe_m.group(1).upper() if dpe_m else None

    return {
        "source": "era",
        "url": url,
        "id_annonce": str(era_id),
        "titre": (item.get("libelle") or f"Maison — {ville} ({cp})")[:150],
        "type_bien": "maison",
        "description": (item.get("descriptif") or "")[:500],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": float(surface) if surface else None,
        "surface_terrain": float(surface_terrain) if surface_terrain else None,
        "pieces": item.get("nb_pieces") or None,
        "chambres": item.get("nb_chambres") or None,
        "prix": float(prix),
        "photos": photos,
        "dpe": dpe,
        "agence": agence[:100],
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria
    criteres = load_criteria()
    biens = asyncio.run(search({
        "departements": criteres.departements[:4],
        "prix_max": criteres.prix_max,
        "prix_min": getattr(criteres, "prix_min", 0),
        "surface_min": criteres.surface_min,
    }))
    print(f"\nTotal ERA: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

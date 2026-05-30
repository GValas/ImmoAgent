"""
scrapers/meilleursbiens.py — MeilleursBiens (réseau de mandataires)

Méthode : api_inoff (httpx) — API JSON interne du portail Next.js
Endpoint : POST https://api.meilleursbiens.com/api/v2/portal/neo/properties/search?page=N&size=100
  Body    : {"type":["V"],"category":["maison"],"hasSelled":true}
            type=["V"] = Vente (achat) ; category=["maison"] = maisons/villas
  Réponse : data.data = liste paginée (envelope Laravel), data.total / data.last_page

POINT CRITIQUE — filtre département :
  L'API n'a PAS de filtre localisation fonctionnel (city/departement/bounds ignorés ou en
  erreur 500). On récupère donc l'inventaire NATIONAL des maisons à vendre (~4200 biens,
  ~43 pages) et on POST-FILTRE par code_postal[:2] ∈ departements (comme remax/era).
  Le champ `departement` de l'API est incohérent (tantôt "05", tantôt "Eure-et-Loir",
  tantôt un code postal) → on se fie UNIQUEMENT à `postal_code`.

Photos  : objets {file, file_cache, file_compressed} sur https://neo.mbiens.pictures
Détail  : https://meilleursbiens.com/annonce/{id}  (id = UUID)
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio

import httpx

BASE_URL = "https://meilleursbiens.com"
API_URL = "https://api.meilleursbiens.com/api/v2/portal/neo/properties/search"

PAGE_SIZE = 100        # plafond serveur (size>100 ignoré)
MAX_PAGES = 50         # ~43 pages réelles pour les maisons à vendre

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/acheter",
}

# Body de recherche : maisons à vendre, inventaire national
SEARCH_BODY = {"type": ["V"], "category": ["maison"], "hasSelled": True}


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    all_items = await _fetch_all()

    results: list[dict] = []
    seen: set[str] = set()
    for item in all_items:
        cp = str(item.get("postal_code") or "")
        dept_item = cp[:2] if len(cp) >= 2 and cp[:2].isdigit() else ""

        # POST-FILTRE département strict par code postal
        if departements and dept_item not in departements:
            continue

        bien = _parse_item(item, dept_item)
        if not bien:
            continue

        # Filtres prix / surface
        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        aid = bien["id_annonce"]
        if aid in seen:
            continue
        seen.add(aid)
        results.append(bien)

    # Log par département
    by_dept: dict[str, list] = {}
    for b in results:
        by_dept.setdefault(b["departement"], []).append(b)
    for dept, items in sorted(by_dept.items()):
        print(f"[MeilleursBiens] Dept {dept}: {len(items)} annonces")

    return results


async def _fetch_all() -> list[dict]:
    all_items: list[dict] = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=40, follow_redirects=True) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{API_URL}?page={page}&size={PAGE_SIZE}"
            try:
                r = await client.post(url, json=SEARCH_BODY)
                if r.status_code != 200:
                    break
                payload = r.json()
            except Exception as e:
                print(f"[MeilleursBiens] Erreur page {page}: {e}")
                break

            data = payload.get("data")
            if not isinstance(data, dict):
                break

            rows = data.get("data", [])
            if not rows:
                break
            all_items.extend(rows)

            last_page = data.get("last_page") or 0
            if page >= last_page:
                break

            await asyncio.sleep(0.3)

    return all_items


def _parse_item(item: dict, dept: str) -> dict | None:
    try:
        uid = str(item.get("id") or "")
        if not uid:
            return None

        url = f"{BASE_URL}/annonce/{uid}"

        prix = item.get("price_public") or item.get("price_rent") or item.get("price_net_selling")
        surface = item.get("surface") or item.get("surface_carrez")
        terrain = item.get("surface_terrain")
        pieces = item.get("nb_pieces")
        chambres = item.get("nb_chambres")

        dpe = item.get("dpe_letter") or None

        pt = item.get("property_type") or {}
        type_label = (pt.get("label") or "maison").lower()

        # Photos : préférer la version web compressée, sinon raw
        photos: list[str] = []
        for pic in (item.get("pictures") or [])[:10]:
            if not isinstance(pic, dict):
                continue
            src = pic.get("file_compressed") or pic.get("file_cache") or pic.get("file")
            if src and src.startswith("http"):
                photos.append(src)

        ville = item.get("city") or ""
        cp = str(item.get("postal_code") or "")

        # Coordonnées : champ GeoJSON {"type":"Point","coordinates":[lon, lat]}.
        lat = lon = None
        geo = item.get("coordinates")
        if isinstance(geo, dict) and isinstance(geo.get("coordinates"), (list, tuple)) and len(geo["coordinates"]) == 2:
            lon, lat = geo["coordinates"][0], geo["coordinates"][1]
            try:
                lat, lon = float(lat), float(lon)
            except (TypeError, ValueError):
                lat = lon = None

        titre = (item.get("title") or "").strip()
        if not titre:
            titre = f"{type_label.capitalize()} {ville}".strip()

        return {
            "source": "meilleursbiens",
            "url": url,
            "id_annonce": uid,
            "titre": titre[:150],
            "type_bien": "maison",
            "description": (item.get("description") or "").strip()[:1200],
            "departement": dept or "??",
            "ville": str(ville)[:80],
            "code_postal": cp,
            "latitude": lat,
            "longitude": lon,
            "surface": float(surface) if surface else None,
            "surface_terrain": float(terrain) if terrain else None,
            "pieces": int(pieces) if pieces else None,
            "chambres": int(chambres) if chambres else None,
            "prix": float(prix) if prix else None,
            "photos": photos,
            "dpe": dpe,
            "agence": "MeilleursBiens",
        }
    except Exception:
        return None


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    biens = asyncio.run(
        search({
            "departements": criteres.departements,
            "prix_max": criteres.prix_max,
            "prix_min": getattr(criteres, "prix_min", 0),
            "surface_min": criteres.surface_min,
        })
    )
    print(f"\nTotal MeilleursBiens: {len(biens)} annonces")
    depts_vus = sorted({b["departement"] for b in biens})
    print(f"Départements vus: {depts_vus}")
    for b in biens[:8]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€ — {b.get('surface')}m² — {b['ville']}"
        )

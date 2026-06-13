"""
scrapers/bienici.py — Bien'ici (API REST directe httpx)
Méthode : httpx direct avec zone IDs par département
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import json

import httpx

BASE_URL = "https://www.bienici.com"
API_URL = f"{BASE_URL}/realEstateAds.json"

# Zone IDs Bienici par département.
# Résolus via l'API de suggestion : https://res.bienici.com/suggest.json?q={nom}
#   (filtrer type == "department" et name == nom officiel exact)
# Les 11 départements cibles sont vérifiés ; en cas de doute, re-résoudre via suggest.json.
# Un zone ID erroné est de toute façon neutralisé par le post-filtre _bien_in_dept().
DEPT_ZONE_IDS = {
    # ── Départements cibles (vérifiés) ──
    "72": ["-7443"],   # Sarthe
    "28": ["-7374"],   # Eure-et-Loir
    "45": ["-7440"],   # Loiret
    "89": ["-7392"],   # Yonne
    "49": ["-7409"],   # Maine-et-Loire
    "37": ["-7408"],   # Indre-et-Loire
    "36": ["-7417"],   # Indre
    "18": ["-7456"],   # Cher
    "58": ["-7448"],   # Nièvre
    "41": ["-7399"],   # Loir-et-Cher
    "53": ["-7438"],   # Mayenne
    # ── Autres (non vérifiés — re-résoudre via suggest.json si activés) ──
    "61": ["-7419"],   # Orne
    "86": ["-7480"],   # Vienne
    "79": ["-7451"],   # Deux-Sèvres
    "85": ["-7479"],   # Vendée
    "44": ["-7413"],   # Loire-Atlantique
    "87": ["-7481"],   # Haute-Vienne
    "23": ["-7372"],   # Creuse
    "03": ["-7360"],   # Allier
    "63": ["-7453"],   # Puy-de-Dôme
}

PAGE_SIZE = 24
MAX_PAGES = 8  # 192 résultats max par département

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": f"{BASE_URL}/",
}


async def search(criteres: dict) -> list[dict]:
    departements = criteres.get("departements", [])
    prix_max = criteres.get("prix_max", 600000)
    surface_min = criteres.get("surface_min", 80)

    results = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        for dept in departements:
            dept_str = str(dept).zfill(2)
            zone_ids = DEPT_ZONE_IDS.get(dept_str)
            if not zone_ids:
                print(f"[Bienici] Dept {dept}: zone ID inconnu, ignoré")
                continue
            try:
                biens = await _fetch_dept(client, dept_str, zone_ids, prix_max, surface_min)
                results.extend(biens)
                print(f"[Bienici] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Bienici] Erreur dept {dept}: {e}")

    return results


def _bien_in_dept(bien: dict, dept: str) -> bool:
    """
    Vrai si le bien appartient réellement au département demandé.
    Priorité au préfixe du code postal (fiable) ; repli sur departement.
    Neutralise les zone IDs erronés qui renverraient des annonces hors-zone.
    """
    cp = str(bien.get("code_postal") or "").strip()
    if len(cp) >= 2 and cp[:2].isdigit():
        return cp[:2] == dept
    return str(bien.get("departement") or "").strip().zfill(2) == dept


async def _fetch_dept(client: httpx.AsyncClient, dept: str, zone_ids: list[str],
                      prix_max: int, surface_min: int) -> list[dict]:
    results = []
    hors_zone = 0

    for page_num in range(MAX_PAGES):
        filters = {
            "size": PAGE_SIZE,
            "from": page_num * PAGE_SIZE,
            "filterType": "buy",
            "propertyType": ["house"],
            "minArea": surface_min,
            "maxPrice": prix_max,
            "onTheMarket": [True],
            "newProperty": False,
            "zoneIdsByTypes": {"zoneIds": zone_ids},
        }
        params = {"filters": json.dumps(filters, separators=(",", ":"))}

        r = await client.get(API_URL, params=params)
        if r.status_code != 200:
            break

        data = r.json()
        ads = data.get("realEstateAds", [])
        if not ads:
            break

        for ad in ads:
            bien = _parse_ad(ad, dept)
            if not bien:
                continue
            if _bien_in_dept(bien, dept):
                results.append(bien)
            else:
                hors_zone += 1

        total = data.get("total", 0)
        fetched_so_far = (page_num + 1) * PAGE_SIZE
        if fetched_so_far >= total:
            break

    if hors_zone:
        print(f"[Bienici] Dept {dept}: {hors_zone} annonces hors-zone écartées "
              f"(zone ID à vérifier via suggest.json ?)")
    return results


def _parse_ad(ad: dict, dept: str) -> dict | None:
    try:
        photos = []
        for photo in ad.get("photos", [])[:10]:
            url = photo.get("url_1024") or photo.get("url_600") or photo.get("url", "")
            if url:
                photos.append(url)

        prix = ad.get("price")
        surface = ad.get("surfaceArea")
        terrain = ad.get("landSurfaceArea")

        dpe = None
        dpe_obj = ad.get("dpe")
        if isinstance(dpe_obj, dict):
            dpe = dpe_obj.get("letter")
        elif isinstance(dpe_obj, str):
            dpe = dpe_obj
        if not dpe:
            dpe = ad.get("energyClassification")

        ad_id = str(ad.get("id", ""))
        ad_url = f"{BASE_URL}/annonce/{ad_id}"

        # Position approximative (floutée) : Bien'ici expose un centre + un rayon
        # de floutage. Exploité par scrapers/geolocate.py pour la pré-localisation.
        lat = lon = blur_radius = None
        blur = ad.get("blurInfo") or {}
        center = blur.get("centroid") or blur.get("position") or {}
        if isinstance(center, dict) and center.get("lat") is not None:
            lat = float(center["lat"])
            lon = float(center["lon"])
            blur_radius = blur.get("radius")

        return {
            "source": "bienici",
            "url": ad_url,
            "id_annonce": ad_id,
            "has_pool": ad.get("hasPool") or False,
            "latitude": lat,
            "longitude": lon,
            "blur_radius_m": float(blur_radius) if blur_radius else None,
            "titre": ad.get("title", ""),
            "type_bien": "maison",
            "description": ad.get("description", "")[:1200],
            "departement": str(ad.get("departmentCode", dept)),
            "ville": ad.get("city", ""),
            "code_postal": str(ad.get("postalCode", "")),
            "surface": float(surface) if surface else None,
            "surface_terrain": float(terrain) if terrain else None,
            "pieces": ad.get("roomsQuantity"),
            "chambres": ad.get("bedroomsQuantity"),
            "prix": float(prix) if prix else None,
            "photos": photos,
            "dpe": dpe,
            "agence": ad.get("agencyName", ""),
            "date_publication": str(ad.get("publicationDate", ""))[:10] or None,
        }
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria
    criteres = load_criteria()
    biens = asyncio.run(search({
        "departements": criteres.departements[:2],
        "prix_max": criteres.prix_max,
        "surface_min": criteres.surface_min,
    }))
    print(f"\nTotal Bienici: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

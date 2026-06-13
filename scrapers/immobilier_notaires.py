"""
scrapers/immobilier_notaires.py — Immobilier.notaires.fr (API officielle)
Méthode : REST API httpx (pas de Playwright nécessaire)
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio

import httpx

BASE_URL = "https://www.immobilier.notaires.fr"
API_URL = f"{BASE_URL}/pub-services/inotr-www-annonces/v1/annonces"
MEDIA_URL = "https://media.immobilier.notaires.fr"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": f"{BASE_URL}/",
}

MAX_PAGES = 8  # 24 results/page → 192 max per dept


async def search(criteres: dict) -> list[dict]:
    departements = criteres.get("departements", [])
    prix_max = criteres.get("prix_max", 600000)
    surface_min = criteres.get("surface_min", 80)

    results = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        for dept in departements:
            try:
                biens = await _fetch_dept(client, str(dept), prix_max, surface_min)
                results.extend(biens)
                print(f"[ImmobilierNotaires] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[ImmobilierNotaires] Erreur dept {dept}: {e}")

    return results


async def _fetch_dept(client: httpx.AsyncClient, dept: str,
                      prix_max: int, surface_min: int) -> list[dict]:
    results = []
    page = 1

    while page <= MAX_PAGES:
        params = {
            "offset": str((page - 1) * 24),
            "page": str(page),
            "parPage": "24",
            "perimetre": "0",
            "departements": dept,
            "typeTransactions": "VENTE",
            "typesBiens": "MAISON",
            "prixMax": str(prix_max),
            "surfaceMin": str(surface_min),
        }
        r = await client.get(API_URL, params=params)
        if r.status_code != 200:
            break

        data = r.json()
        ads = data.get("annonceResumeDto", [])
        if not ads:
            break

        for ad in ads:
            bien = _parse_ad(ad, dept)
            if bien:
                results.append(bien)

        nb_pages = data.get("nbPages", 1)
        if page >= nb_pages:
            break
        page += 1

    return results


def _parse_ad(ad: dict, dept: str) -> dict | None:
    try:
        type_bien = ad.get("typeBien", "")
        if type_bien not in ("MAI", "MAIS", "VIL", "CHA", "MAN"):
            return None

        annonce_id = str(ad.get("annonceId", ""))
        url = ad.get("urlDetailAnnonceFr", "")
        if not url:
            return None

        prix = ad.get("prixAffiche")
        surface = ad.get("surface")
        terrain = ad.get("surfaceTerrain")
        pieces = ad.get("nbPieces")
        chambres = ad.get("nbChambres")
        description = ad.get("descriptionFr", "")[:1200]

        ville = ad.get("communeNom", ad.get("localiteNom", ""))
        code_postal = ad.get("codePostal", "")

        # Photo principale
        photos = []
        photo_url = ad.get("urlPhotoPrincipale", "")
        if photo_url:
            photos.append(photo_url)

        titre = f"Maison {pieces} p. {surface}m² {ville} ({code_postal})"

        return {
            "source": "immobilier_notaires",
            "url": url,
            "id_annonce": annonce_id,
            "titre": titre,
            "type_bien": "maison",
            "description": description,
            "departement": ad.get("inseeDepartement", dept),
            "ville": ville,
            "code_postal": code_postal,
            "surface": float(surface) if surface else None,
            "surface_terrain": float(terrain) if terrain else None,
            "pieces": int(pieces) if pieces else None,
            "chambres": int(chambres) if chambres else None,
            "prix": float(prix) if prix else None,
            "photos": photos,
            "dpe": None,
            "agence": f"Notaire - ref {ad.get('reference', '')}",
            "date_publication": ad.get("dateCreation", "")[:10] or None,
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
    print(f"\nTotal ImmobilierNotaires: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

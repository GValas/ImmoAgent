"""scrapers/trackstone.py — Trackstone (proptech, vente de biens loués à investisseurs)

Méthode : scrape_simple (httpx) — SSR Inertia.js
URL pattern : /biens?page=N   (listing NATIONAL, pas de filtre département serveur)
              → on scrape toutes les pages puis on POST-FILTRE sur code_postal[:2].

Données : la page injecte tout le state Inertia dans
  <script type="application/json"> … </script>  →  data['props']['listings']['data']
  (liste de 30 biens/page ; meta.last_page donne le nombre de pages).

Chaque item :
  - reference          → id_annonce (ex: "TRA1256F1")
  - property.slug      → /biens/{slug}/{reference}  (slug = {rue}-{CP}-{ville}-{type})
  - property.type      → "Appartement" / "Maison" / "Immeuble" / "Local commercial"...
  - property.postal_code, property.city, property.full_address
  - property.total_rooms (pièces), property.lot_surface (surface m²)
  - property.energy_level_class → DPE
  - property.center {lat,lng}, property.cover_thumb_url (photo)
  - listing_price.price → prix de vente affiché

Particularité : portail de biens LOUÉS revendus à des investisseurs (rendement
locatif). Couverture nationale, implantation faible en Val-de-Loire/Ouest :
sur la zone cible le stock est marginal mais réel (dépt 28, 53 vus). Pas de
filtre département côté serveur → 0 fuite assurée par le post-filtre CP[:2].

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.trackstone.fr"
MAX_PAGES = 15  # garde-fou ; le site renvoie ~9 pages (245 biens / 30)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        page = 1
        last_page = MAX_PAGES
        while page <= min(last_page, MAX_PAGES):
            url = f"{BASE_URL}/biens?page={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Trackstone] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            listings, last_page = _extract_listings(r.text)
            if not listings:
                break

            for item in listings:
                bien = _parse_item(item, departements)
                if not bien:
                    continue
                # Post-filtre département STRICT (aucun filtre serveur)
                if not bien["code_postal"] or bien["code_postal"][:2] not in departements:
                    continue
                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                seen_ids.add(aid)
                results.append(bien)

            page += 1
            await asyncio.sleep(0.5)

    print(f"[Trackstone] {len(results)} annonces dans la zone")
    return results


def _extract_listings(html: str) -> tuple[list, int]:
    """Renvoie (liste des items, last_page) depuis le state Inertia embarqué."""
    soup = BeautifulSoup(html, "html.parser")
    el = soup.find("script", type="application/json")
    if not el or not el.string:
        return [], 0
    try:
        data = json.loads(el.string)
    except (json.JSONDecodeError, TypeError):
        return [], 0
    listings = data.get("props", {}).get("listings", {})
    items = listings.get("data", []) or []
    last_page = int(listings.get("meta", {}).get("last_page", 0) or 0)
    return items, last_page


def _parse_item(item: dict, departements: set[str]) -> dict | None:
    prop = item.get("property") or {}
    slug = prop.get("slug") or ""
    ref = item.get("reference") or ""
    if not slug or not ref:
        return None

    code_postal = (prop.get("postal_code") or "").strip()
    # Filtre rapide avant tout parsing coûteux
    if code_postal and code_postal[:2] not in departements:
        return None

    url = f"{BASE_URL}/biens/{slug}/{ref}"
    dept = code_postal[:2] if code_postal else None

    ville = (prop.get("city") or "").strip()
    type_bien = (prop.get("type") or "").strip() or "bien"

    pieces = prop.get("total_rooms")
    surface = prop.get("lot_surface")
    dpe = prop.get("energy_level_class") or None

    price = (item.get("listing_price") or {}).get("price")
    try:
        prix = float(price) if price is not None else None
    except (ValueError, TypeError):
        prix = None

    addr = (prop.get("full_address") or prop.get("street_address") or "").strip()
    titre = f"{type_bien} {ville}".strip()
    if surface:
        titre = f"{type_bien} {surface} m² {ville}".strip()

    photos: list[str] = []
    cover = prop.get("cover_thumb_url")
    if cover and not str(cover).startswith("data:"):
        photos.append(cover)

    return {
        "source": "trackstone",
        "url": url,
        "id_annonce": ref,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": addr[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": float(surface) if surface else None,
        "surface_terrain": None,
        "pieces": int(pieces) if pieces else None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Trackstone",
    }


# ── CLI standalone ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    biens = asyncio.run(
        search(
            {
                "departements": criteres.departements,
                "prix_max": criteres.prix_max,
                "prix_min": getattr(criteres, "prix_min", 0),
                "surface_min": criteres.surface_min,
            }
        )
    )
    print(f"\nTotal Trackstone: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p"
            f" — DPE {b['dpe'] or '?'}"
            f" — {b['type_bien']} — {b['ville']}"
        )

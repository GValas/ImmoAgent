"""scrapers/bsk_immobilier.py — BSK Immobilier (réseau national de mandataires, ~4400)

Méthode : api_inoff (httpx) — API JSON interne du portail B2C.
Endpoint : GET https://bskimmobilier.com/api/property/search
  Paramètres (form serialisé) :
    page[size], page[number]
    search[transaction_type][]=SALE
    search[property_type][]=HOUSE | CASTLE | …    (multi)
    search[min_price], search[max_price], search[min_surface]

Filtre département : le filtre serveur par localisation (location[type]=department)
  renvoie une erreur 500 côté API → INUTILISABLE. On interroge donc le flux
  NATIONAL pré-filtré (type maison/château + bornes prix/surface, ce qui réduit
  fortement le volume : ~60 pages de 50) et on POST-FILTRE strictement sur
  city.zipCode[:2] ∈ départements cibles → 0 fuite garantie.

Réponse JSON : data[] (dicts riches), meta.last_page (pagination).
  Champs par item utilisés :
    title, description, propertyType, url
    city.{name, zipCode}
    priceInclFees, surface, surfaceTerrain, roomsCount, bedroomsCount
    energyConsumptionClass (DPE), photos[].url, agent.html (nom mandataire)

Couverture : réseau national ; stock zone cible faible mais réel.

Flux NATIONAL sans boucle département → les drivers de _base ne s'appliquent
pas ; on utilise le socle pour le client (make_client), le retry 429/503
(get_with_retry — l'API throttle), le post-filtre (keep_bien) et le CLI
(standalone_main).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

from scrapers._base import HEADERS as _BASE_HEADERS
from scrapers._base import get_with_retry, keep_bien, make_client, standalone_main

BASE_URL = "https://bskimmobilier.com"
API_URL = f"{BASE_URL}/api/property/search"
PAGE_SIZE = 50
MAX_PAGES = 80          # garde-fou ; on s'arrête à meta.last_page de toute façon
PHOTOS_PER_CARD = 10

# Types BSK à conserver (maisons / propriétés / châteaux)
KEEP_PROPERTY_TYPES = ["HOUSE", "CASTLE"]

# En-têtes AJAX de l'API interne (UA du socle + spécifiques XHR)
HEADERS = {
    **_BASE_HEADERS,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE_URL}/acheter",
}

# Libellé FR du type BSK → type_bien normalisé
_TYPE_LABEL = {
    "HOUSE": "maison",
    "CASTLE": "chateau",
    "APARTMENT": "appartement",
    "BUILDING": "immeuble",
    "TERRAIN": "terrain",
}


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    # Paramètres serveur (réduit le volume national à parcourir)
    base_params: list[tuple[str, str]] = [
        ("page[size]", str(PAGE_SIZE)),
        ("search[transaction_type][]", "SALE"),
    ]
    for t in KEEP_PROPERTY_TYPES:
        base_params.append(("search[property_type][]", t))
    if prix_min:
        base_params.append(("search[min_price]", str(prix_min)))
    if prix_max:
        base_params.append(("search[max_price]", str(prix_max)))
    if surface_min:
        base_params.append(("search[min_surface]", str(surface_min)))

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with make_client(timeout=40, headers=HEADERS) as client:
        last_page = MAX_PAGES
        for page in range(1, MAX_PAGES + 1):
            params = base_params + [("page[number]", str(page))]
            r = await get_with_retry(client, API_URL, params=params)
            if r is None:
                print(f"[BSK] Erreur page {page}: réseau")
                break
            if r.status_code != 200:
                print(f"[BSK] Stop page {page} (HTTP {r.status_code})")
                break

            data = r.json()
            meta = data.get("meta", {})
            last_page = meta.get("last_page", last_page)
            rows = data.get("data", [])
            if not rows:
                break

            for it in rows:
                try:
                    bien = _parse_item(it, departements)
                except Exception:
                    continue
                if not bien:
                    continue
                # Dédup + bornes (re-vérifiées même si filtrées serveur) ;
                # dept=None : le filtre strict zipCode est déjà dans _parse_item
                if not keep_bien(bien, None, seen_ids, prix_max=prix_max,
                                 prix_min=prix_min, surface_min=surface_min):
                    continue
                results.append(bien)

            if page >= last_page:
                break
            await asyncio.sleep(0.7)  # poli : l'API renvoie 429 si on tape trop vite

    # Récap par département (info)
    from collections import Counter
    cnt = Counter(b["code_postal"][:2] for b in results if b["code_postal"])
    print(f"[BSK] {len(results)} annonces in-zone (last_page={last_page}) — {dict(cnt)}")
    return results


def _parse_item(it: dict, departements: set[str]) -> dict | None:
    city = it.get("city") or {}
    code_postal = (city.get("zipCode") or "").strip()
    if not code_postal or code_postal[:2] not in departements:
        return None  # POST-FILTRE STRICT — 0 fuite

    dept = code_postal[:2]
    ville = (city.get("name") or "").strip()

    prop_type = (it.get("propertyType") or "").upper()
    if prop_type not in KEEP_PROPERTY_TYPES:
        return None
    type_bien = _TYPE_LABEL.get(prop_type, "maison")

    url = it.get("url") or ""
    if url:
        url = url.split("?")[0]
    aid = str(it.get("id") or it.get("uuid") or url)

    titre = (it.get("title") or "").strip() or f"{type_bien.title()} {ville}".strip()
    description = (it.get("description") or "").strip()

    prix = _num(it.get("priceInclFees"))
    surface = _num(it.get("surface"))
    surface_terrain = _num(it.get("surfaceTerrain"))
    pieces = _int(it.get("roomsCount"))
    chambres = _int(it.get("bedroomsCount"))
    dpe = (it.get("energyConsumptionClass") or None) or None

    photos = []
    for ph in it.get("photos") or []:
        u = ph.get("url") if isinstance(ph, dict) else None
        if u and u.startswith("http"):
            photos.append(u)
    photos = photos[:PHOTOS_PER_CARD]

    agence = _agent_name(it.get("agent"))

    return {
        "source": "bsk_immobilier",
        "url": url,
        "id_annonce": aid,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": agence or "BSK Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _num(v) -> float | None:
    try:
        if v in (None, "", 0):
            return None if v in (None, "") else 0.0
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    try:
        if v in (None, ""):
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _agent_name(agent) -> str | None:
    """Le mandataire est dans agent.html (bloc HTML). On en extrait le nom propre."""
    if not isinstance(agent, dict):
        return None
    html = agent.get("html") or ""
    # Premier texte en MAJUSCULES type "PRENOM NOM"
    m = re.search(r">\s*([A-ZÉÈÀÂÊ][A-ZÉÈÀÂÊ\- ]{3,40})\s*<", html)
    if m:
        name = re.sub(r"\s+", " ", m.group(1)).strip().title()
        if name and not name.lower().startswith("voir"):
            return f"BSK — {name}"
    return None


if __name__ == "__main__":
    standalone_main(search, "BSK")

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

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx

BASE_URL = "https://bskimmobilier.com"
API_URL = f"{BASE_URL}/api/property/search"
PAGE_SIZE = 50
MAX_PAGES = 80          # garde-fou ; on s'arrête à meta.last_page de toute façon
PHOTOS_PER_CARD = 10

# Types BSK à conserver (maisons / propriétés / châteaux)
KEEP_PROPERTY_TYPES = ["HOUSE", "CASTLE"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "fr-FR,fr;q=0.9",
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

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=40
    ) as client:
        last_page = MAX_PAGES
        for page in range(1, MAX_PAGES + 1):
            params = base_params + [("page[number]", str(page))]
            try:
                r = await client.get(API_URL, params=params)
            except Exception as e:
                print(f"[BSK] Erreur page {page}: {e}")
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
                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue
                # Bornes (re-vérifiées même si filtrées serveur)
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
    print(f"\nTotal BSK: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['pieces'] or '?'}p — DPE {b['dpe'] or '?'} — {b['ville']}"
        )

"""scrapers/casadici.py — Casadici (mandataire / réseau, plateforme Netty.immo)

Méthode : scrape_simple (httpx) — SSR React, données dans le HTML brut.
URL pattern de recherche : /vente/{type}/{ville-slug}/{cp}
              (ex: /vente/maison/bourges/18000)
              → recherche par VILLE avec un rayon élargi (renvoie aussi les
                communes voisines), PAS un filtre département serveur fiable.
              On itère donc sur des villes-ancres par département cible, PUIS on
              post-filtre STRICTEMENT sur cp[:2] == dept (0 fuite hors-zone).

Rendu : la page est servie côté serveur par React, mais les annonces ne sont
        PAS dans des balises HTML lisibles : elles sont sérialisées en JSON
        base64 dans `window._TEMPLATE_DATA` (script inline). On décode ce blob :
          - _TEMPLATE_DATA["prodId"]      : dict {ref -> produit complet}
          - _TEMPLATE_DATA["prodResults"]["search"] : liste des refs RÉSULTATS
            de la recherche (≠ des produits "front"/mis en avant cross-promo qui
            polluent prodId avec des biens hors-zone → on les ignore).
        Pas de JS / Playwright nécessaire, pas d'appel API supplémentaire.

Champs produit (clés du JSON Netty) :
  - prod_ref / ref          : référence (id_annonce)
  - title["fr"]             : titre commercial ; title_auto : titre généré
  - prod_type               : house / appt / land (on garde house)
  - city, cp                : ville / code postal → filtre dept
  - pricePrimary / price2   : prix de vente
  - surface                 : surface habitable (m²)
  - land                    : surface terrain (m²)
  - photos                  : liste d'URLs (img.netty.immo)
  - dpe                     : note DPE (souvent None en liste)
  - url["fr"]               : slug détail → /vente/{slug}
  - details["fr"]           : description (HTML)
  - title_auto              : "… N pièces …" → extraction du nb de pièces

Couverture : réseau à implantation très inégale ; dans la zone cible le stock se
             concentre autour de Bourges (18) et La Charité-sur-Loire (58).
             Les autres préfectures renvoient 0 résultat (réel, pas un bug).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import base64
import json
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.casadici.fr"
PHOTOS_PER_CARD = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Segments de type d'URL à interroger (la recherche "maison" couvre maisons +
# propriétés ; on ne requête pas appartement/terrain → on cible le résidentiel).
SEARCH_TYPES = ["maison"]

# Types de bien (clé Netty prod_type) à conserver.
_KEEP_PROD_TYPE = {"house"}

# Villes-ancres par département cible : la recherche par ville renvoie un rayon
# élargi (communes voisines), donc 1 à 3 ancres suffisent à balayer un dept.
# Le post-filtre cp[:2] garantit l'absence de fuite hors-zone.
DEPT_CITIES: dict[str, list[tuple[str, str]]] = {
    "72": [("le-mans", "72000"), ("la-fleche", "72200"), ("sable-sur-sarthe", "72300")],
    "28": [("chartres", "28000"), ("dreux", "28100"), ("chateaudun", "28200")],
    "45": [("orleans", "45000"), ("montargis", "45200"), ("gien", "45500")],
    "89": [("auxerre", "89000"), ("sens", "89100"), ("avallon", "89200")],
    "49": [("angers", "49000"), ("cholet", "49300"), ("saumur", "49400")],
    "37": [("tours", "37000"), ("chinon", "37500"), ("amboise", "37400")],
    "36": [("chateauroux", "36000"), ("issoudun", "36100"), ("le-blanc", "36300")],
    "18": [("bourges", "18000"), ("vierzon", "18100"), ("saint-amand-montrond", "18200")],
    "58": [("nevers", "58000"), ("la-charite-sur-loire", "58400"), ("cosne-cours-sur-loire", "58200")],
    "41": [("blois", "41000"), ("vendome", "41100"), ("romorantin-lanthenay", "41200")],
    "53": [("laval", "53000"), ("mayenne", "53100"), ("chateau-gontier", "53200")],
}


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            cities = DEPT_CITIES.get(dept)
            if not cities:
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, cities, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[Casadici] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Casadici] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.5)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    cities: list[tuple[str, str]],
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_refs: set[str] = set()

    for ville_slug, cp in cities:
        for type_seg in SEARCH_TYPES:
            url = f"{BASE_URL}/vente/{type_seg}/{ville_slug}/{cp}"
            try:
                r = await client.get(url)
            except Exception:
                continue
            if r.status_code != 200:
                continue

            data = _extract_template_data(r.text)
            if not data:
                continue

            prod_id = data.get("prodId") or {}
            # Refs AUTHENTIQUES de la recherche (≠ produits "front" cross-promo).
            search_refs = (data.get("prodResults") or {}).get("search") or []

            for ref in search_refs:
                prod = prod_id.get(ref)
                if not prod:
                    continue
                if ref in seen_refs:
                    continue

                bien = _parse_product(prod, dept)
                if not bien:
                    continue

                # Post-filtre STRICT département (0 fuite : la recherche par
                # ville déborde sur les communes voisines).
                if not bien["code_postal"] or bien["code_postal"][:2] != dept:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                seen_refs.add(ref)
                biens.append(bien)

            await asyncio.sleep(0.4)

    return biens


def _extract_template_data(html: str) -> dict | None:
    """Décode window._TEMPLATE_DATA (JSON base64 dans un <script> inline)."""
    m = re.search(
        r'_TEMPLATE_DATA\s*=\s*JSON\.parse\(b64_to_utf8\("([^"]+)"\)\)', html
    )
    if not m:
        return None
    try:
        raw = base64.b64decode(m.group(1)).decode("utf-8")
        return json.loads(raw)
    except Exception:
        return None


def _parse_product(prod: dict, dept: str) -> dict | None:
    prod_type = prod.get("prod_type")
    if prod_type not in _KEEP_PROD_TYPE:
        return None

    ref = prod.get("prod_ref") or prod.get("ref")
    if not ref:
        return None

    city = (prod.get("city") or "").strip()
    cp = str(prod.get("cp") or "").strip()

    # Titre : commercial puis auto en secours
    titre = _localized(prod.get("title")) or prod.get("title_auto") or ""
    if not titre:
        titre = f"Maison {city}".strip()

    # URL détail : /vente/{slug-fr}
    slug = _localized(prod.get("url"))
    url = f"{BASE_URL}/vente/{slug}" if slug else f"{BASE_URL}/"

    # Description (HTML → texte)
    details = _localized(prod.get("details")) or ""
    description = BeautifulSoup(details, "html.parser").get_text(" ", strip=True)

    # Prix
    prix = _to_float(prod.get("pricePrimary"))
    if prix is None:
        prix = _to_float(prod.get("price2")) or _to_float(prod.get("price1"))

    # Surfaces
    surface = _to_float(prod.get("surface"))
    surface_terrain = _to_float(prod.get("land"))

    # Pièces : roomsList sinon "N pièces" dans title_auto/titre
    pieces = None
    rooms = prod.get("roomsList")
    if isinstance(rooms, list) and rooms:
        pieces = len(rooms)
    if pieces is None:
        pieces = _parse_int(r"(\d+)\s*pi[eè]ces?", prod.get("title_auto") or titre)

    chambres = _parse_int(r"(\d+)\s*chambres?", description) or _parse_int(
        r"(\d+)\s*chambres?", prod.get("title_auto") or ""
    )

    # Photos
    photos: list[str] = []
    raw_photos = prod.get("photos")
    if isinstance(raw_photos, list):
        for ph in raw_photos:
            if isinstance(ph, str) and ph.startswith("http"):
                photos.append(ph)
    if not photos:
        for ph in prod.get("photos_object") or []:
            u = ph.get("url") if isinstance(ph, dict) else None
            if u:
                photos.append(u)
    photos = photos[:PHOTOS_PER_CARD]

    # DPE (souvent absent en liste)
    dpe = prod.get("dpe")
    if isinstance(dpe, str):
        dpe = dpe.strip().upper() or None
        if dpe and dpe not in {"A", "B", "C", "D", "E", "F", "G"}:
            dpe = None
    else:
        dpe = None

    return {
        "source": "casadici",
        "url": url,
        "id_annonce": ref,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": description[:1200],
        "departement": dept,
        "ville": city[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Casadici",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _localized(val) -> str:
    """Champ Netty localisé : {'fr': '...'} ou chaîne directe."""
    if isinstance(val, dict):
        return (val.get("fr") or next(iter(val.values()), "") or "").strip()
    if isinstance(val, str):
        return val.strip()
    return ""


def _to_float(val) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) if val else None
    cleaned = re.sub(r"[^\d.]", "", str(val).replace(",", "."))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    if not text:
        return None
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


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
    print(f"\nTotal Casadici: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b.get('pieces') or '?'}p — {b['ville']}"
        )

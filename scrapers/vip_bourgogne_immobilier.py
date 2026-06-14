"""scrapers/vip_bourgogne_immobilier.py — VIP Bourgogne Immobilier

Méthode : api_inoff — API WP REST (WordPress / thème RealHomes), pas de Playwright.
Site : https://www.vip-bourgogne-immobilier.com
Agence du nord-Nièvre / sud-Yonne (Clamecy, Varzy, Tannay, Corbigny…).

URL pattern :
  - Biens   : /wp-json/wp/v2/property?per_page=50&page=N  (JSON, paginé)
  - Cantons : /wp-json/wp/v2/property-city?per_page=100   (taxonomie IDs→noms)

ATTENTION : un plugin injecte 2 lignes <link ...css> AVANT le JSON.
  → on fait txt=r.text ; i=txt.find('[{') ; data=json.loads(txt[i:]).

Stratégie filtre département (CRITIQUE, 0 fuite exigée) :
  - REAL_HOMES_property_address est VIDE et la lat/long est un placeholder agence
    → INUTILISABLE pour le département. code_postal toujours None.
  - La taxonomie 'property-city' référence des CANTONS (pas des villes). On
    résout chaque canton vers son département réel via le mapping CANTON_DEPT
    (codé en dur, chef-lieu vérifié). Pour chaque bien on prend le PREMIER canton
    qui résout vers un département cible ; sinon le bien est ÉCARTÉ.
  - "Bourgogne" (générique, sans canton précis) → None → bien écarté (prudence).

Interface : async def search(criteres: dict) -> list[dict]
"""
from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from typing import Optional

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client

BASE_URL = "https://www.vip-bourgogne-immobilier.com"
PROPERTY_API = f"{BASE_URL}/wp-json/wp/v2/property"
CITY_API = f"{BASE_URL}/wp-json/wp/v2/property-city"
PER_PAGE = 50
MAX_PAGES = 20
PHOTOS_PER_CARD = 10

# Types non-résidentiels à exclure (regardés dans titre + url).
_EXCLUDE_TYPE = re.compile(
    r"terrain|garage|local|commerc|immeuble|fonds|parking|bureau|entrep[oô]t|hangar",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    """Normalise un nom de canton : retire préfixe 'Canton De/d'', accents,
    casse, et caractères non alphanumériques → clé comparable."""
    s = text or ""
    # retire le préfixe canton
    s = re.sub(r"^\s*canton\s+(de\s+|d['’]\s*)?", "", s, flags=re.IGNORECASE)
    # retire accents
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    # ne garde que [a-z0-9]
    s = re.sub(r"[^a-z0-9]", "", s)
    return s


# Mapping canton (chef-lieu) → département. Clés = noms normalisés via _norm().
_CANTON_DEPT_RAW = {
    # ── 58 (Nièvre) ──
    "Clamecy": "58",
    "Varzy": "58",
    "Tannay": "58",
    "Corbigny": "58",
    "Donzy": "58",
    "Prémery": "58",
    "La Charité Sur Loire": "58",
    "La Charité-sur-Loire": "58",
    "Lormes": "58",
    "Brinon-sur-Beuvron": "58",
    "Brinon sur Beuvron": "58",
    "Champlemy": "58",
    "Dornecy": "58",
    "Brèves": "58",
    "Oudan": "58",
    "Billy sur Oisy": "58",
    "Billy-sur-Oisy": "58",
    "Corvol L'Orgueilleux": "58",
    "Corvol-l'Orgueilleux": "58",
    "Menou": "58",
    "Châteauneuf-Val-de-Bargis": "58",
    "La Chapelle Saint André": "58",
    "La Chapelle-Saint-André": "58",
    "Entrains sur Nohain": "58",
    "Entrains-sur-Nohain": "58",
    "Cosne-sur-Loire": "58",
    "Pousseaux": "58",
    "Nuars": "58",
    "Villiers Sur Yonne": "58",
    "Villiers-sur-Yonne": "58",
    "Etais la Sauvin": "58",
    "Étais-la-Sauvin": "58",
    "Nièvre": "58",
    # ── 89 (Yonne) ──
    "Coulanges-sur-Yonne": "89",
    "Coulanges Sur Yonne": "89",
    "Lichères sur Yonne": "89",
    "Lichères-sur-Yonne": "89",
    "Vézelay": "89",
    "Yonne": "89",
    # ── générique, ambigu → écarté ──
    "Bourgogne": None,
}

# Dict normalisé (clé _norm → dept ou None).
CANTON_DEPT: dict[str, Optional[str]] = {_norm(k): v for k, v in _CANTON_DEPT_RAW.items()}


def _strip_html(html: str) -> str:
    """Nettoie un fragment HTML → texte brut."""
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def _to_float(val) -> Optional[float]:
    if val is None:
        return None
    s = re.sub(r"[^\d.]", "", str(val).replace(",", "."))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _to_int(val) -> Optional[int]:
    f = _to_float(val)
    return int(f) if f is not None else None


def _meta_first(meta: dict, key: str):
    """property_meta stocke parfois les valeurs en liste (WP). Renvoie le scalaire."""
    v = meta.get(key)
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    return v


def _load_json_after_link(text: str):
    """Le plugin injecte des <link ...css> AVANT le JSON. On saute au 1er '[{'."""
    i = text.find("[{")
    if i == -1:
        # liste vide possible : "[]"
        i = text.find("[")
    if i == -1:
        return []
    return json.loads(text[i:])


async def _fetch_city_map(client) -> dict[int, Optional[str]]:
    """Récupère IDs de canton → département résolu (ou None si non-cible/ambigu)."""
    r = await get_with_retry(client, f"{CITY_API}?per_page=100")
    if r is None or r.status_code != 200:
        print("[VIPBourgogne] Echec récupération taxonomie property-city")
        return {}
    try:
        cities = _load_json_after_link(r.text)
    except Exception as e:
        print(f"[VIPBourgogne] JSON property-city illisible: {e}")
        return {}

    city_dept: dict[int, Optional[str]] = {}
    for c in cities:
        cid = c.get("id")
        name = c.get("name") or ""
        dept = CANTON_DEPT.get(_norm(name))  # None si inconnu OU mappé None
        city_dept[cid] = dept
    return city_dept


def _resolve_dept(prop: dict, city_dept: dict[int, Optional[str]],
                  cibles: list[str]) -> tuple[Optional[str], Optional[str]]:
    """Renvoie (departement, nom_canton) du 1er canton résolvant vers un dept
    cible, sinon (None, None)."""
    city_ids = prop.get("property-city") or []
    if isinstance(city_ids, int):
        city_ids = [city_ids]
    for cid in city_ids:
        dept = city_dept.get(cid)
        if dept and dept in cibles:
            return dept, None
    return None, None


def _photos_from_meta(meta: dict) -> list[str]:
    """REAL_HOMES_property_images = liste d'IDs d'attachements (pas d'URL directe
    sans appel supplémentaire). On ne fait pas de requête en plus → []."""
    return []


async def search(criteres: dict) -> list[dict]:
    cibles = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with make_client() as client:
        city_dept = await _fetch_city_map(client)
        if not city_dept:
            print("[VIPBourgogne] Taxonomie cantons vide → aucun filtre dept fiable, arrêt")
            return results

        for page in range(1, MAX_PAGES + 1):
            url = f"{PROPERTY_API}?per_page={PER_PAGE}&page={page}"
            r = await get_with_retry(client, url)
            if r is None or r.status_code != 200:
                break
            try:
                props = _load_json_after_link(r.text)
            except Exception as e:
                print(f"[VIPBourgogne] JSON property illisible page {page}: {e}")
                break
            if not props:
                break

            for prop in props:
                bien = _parse_property(prop, city_dept, cibles)
                if not bien:
                    continue

                # post-filtre dept STRICT (0 fuite)
                if bien["departement"] not in cibles:
                    continue

                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue

                # bornes prix / surface (sans exclure si champ manquant)
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

            if len(props) < PER_PAGE:
                break
            await asyncio.sleep(0.4)

    print(f"[VIPBourgogne] Total {len(results)} annonces (depts cibles {cibles})")
    return results


def _parse_property(prop: dict, city_dept: dict[int, Optional[str]],
                    cibles: list[str]) -> Optional[dict]:
    meta = prop.get("property_meta") or {}

    url = prop.get("link") or ""
    titre = _strip_html((prop.get("title") or {}).get("rendered", ""))
    description = (
        _strip_html((prop.get("excerpt") or {}).get("rendered", ""))
        or _strip_html((prop.get("content") or {}).get("rendered", ""))
    )

    # exclusion types non-résidentiels (titre + url)
    haystack = f"{titre} {url}"
    if _EXCLUDE_TYPE.search(haystack):
        return None

    dept, ville = _resolve_dept(prop, city_dept, cibles)
    if dept is None:
        return None  # aucun canton ne résout vers un dept cible → écarté

    ref = _meta_first(meta, "REAL_HOMES_property_id")
    id_annonce = str(ref) if ref else str(prop.get("id") or url)

    prix = _to_float(_meta_first(meta, "REAL_HOMES_property_price"))
    surface = _to_float(_meta_first(meta, "REAL_HOMES_property_size"))
    surface_terrain = _to_float(_meta_first(meta, "REAL_HOMES_property_lot_size"))
    chambres = _to_int(_meta_first(meta, "REAL_HOMES_property_bedrooms"))

    return {
        "source": "vip_bourgogne_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": description[:1200],
        "departement": dept,
        "ville": ville,
        "code_postal": None,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": _photos_from_meta(meta),
        "dpe": None,
        "agence": "VIP Bourgogne Immobilier",
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "VIP Bourgogne")

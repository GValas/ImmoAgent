"""scrapers/karacterre.py — Karacterre Immobilier (agence biens de caractère, Angers / Le Mans)

Méthode : scrape_simple (httpx) — données SSR embarquées (CMS Netty.immo)
URL pattern : /fr/vente?page=N   (1 page = 10 biens "search", ~13 pages)

Particularité du rendu : le site est un SPA (bundle.site.js, Vue/React) MAIS
les données produits sont injectées CÔTÉ SERVEUR dans le HTML sous la forme
    window._TEMPLATE_DATA = JSON.parse(b64_to_utf8("<base64>"));
→ on récupère le JSON décodé en base64, pas besoin de Playwright ni d'API.
Le bloc `_TEMPLATE_DATA` contient :
  - prodId        : dict {prod_ref: {champs produit}}
  - prodResults['search'] : liste ordonnée des refs de la page courante
  - prodCount     : nombre total de biens

Champs produit utiles : prod_ref, title{fr}, title_auto, details{fr} (description),
  meta_desc{fr}, city, cp (code postal), pricePrimary / price2, surface, land
  (terrain), roomsList (liste de pièces), prod_type (house/appt/build/land/...),
  url{fr} (slug détail), photos (liste d'URL img.netty.immo).

Filtre département : le site NE filtre PAS côté serveur par dept (les query
params ?dpt=… sont ignorés en HTTP brut → renvoie tout). Stratégie : on
parcourt toutes les pages et on POST-FILTRE STRICTEMENT sur cp[:2] ∈ dept cibles.
La couverture inventaire est concentrée sur 49 (Maine-et-Loire) et 72 (Sarthe),
avec quelques biens 37/53 ; le reste (35, 44, 56, 79, 85) est écarté par le filtre.

Détail URL : /fr/vente/{type-slug}/{url.fr}   (type-slug = maison/appartement/...)

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import base64
import json
import re

import httpx

from scrapers._base import HEADERS

BASE_URL = "https://www.karacterre-immobilier.fr"
LISTING_URL = BASE_URL + "/fr/vente"
MAX_PAGES = 16
PHOTOS_PER_CARD = 12


# Slug d'URL de détail par type de bien de premier niveau (prod_type Netty)
_TYPE_URL_SLUG = {
    "house": "maison",
    "appt": "appartement",
    "build": "immeuble",
    "land": "terrain",
    "comm": "fonds-de-commerce",
    "pro": "immobilier-pro",
    "park": "stationnement",
    "bail": "droit-au-bail",
    "ent": "transmission-d-entreprise",
}

# Libellé FR lisible par type (pour le champ type_bien)
_TYPE_LABEL = {
    "house": "maison",
    "appt": "appartement",
    "build": "immeuble",
    "land": "terrain",
    "comm": "commerce",
    "pro": "immobilier pro",
    "park": "stationnement",
    "bail": "droit au bail",
    "ent": "entreprise",
}

# On ne garde que maisons / propriétés (pas d'appartement, immeuble, terrain, pro…)
_KEEP_TYPES = {"house"}


def _b64_template_data(html: str) -> dict | None:
    """Extrait et décode window._TEMPLATE_DATA (base64 → JSON)."""
    m = re.search(
        r'window\._TEMPLATE_DATA\s*=\s*JSON\.parse\(b64_to_utf8\("([^"]+)"\)\)',
        html,
    )
    if not m:
        return None
    try:
        return json.loads(base64.b64decode(m.group(1)).decode("utf-8", "replace"))
    except Exception:
        return None


def _localized(value, lang: str = "fr") -> str:
    """Champs localisés Netty : soit str, soit {'fr': ..., 'en': ...}."""
    if isinstance(value, dict):
        return str(value.get(lang) or value.get("en") or next(iter(value.values()), "") or "")
    return str(value or "")


def _to_int(value) -> int | None:
    if value is None:
        return None
    s = re.sub(r"[^\d]", "", str(value).split(",")[0])
    try:
        return int(s) if s else None
    except ValueError:
        return None


def _to_float(value) -> float | None:
    if value is None:
        return None
    s = str(value).replace(",", ".")
    s = re.sub(r"[^\d.]", "", s)
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _pieces_from_title_auto(title_auto: str) -> int | None:
    m = re.search(r"(\d+)\s*pi[eè]ces?", title_auto, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _detail_url(prod: dict) -> str:
    prod_type = str(prod.get("prod_type") or "house")
    slug = _TYPE_URL_SLUG.get(prod_type, "maison")
    url_slug = _localized(prod.get("url"))
    if not url_slug:
        return BASE_URL + "/fr/vente"
    return f"{BASE_URL}/fr/vente/{slug}/{url_slug}"


def _parse_product(ref: str, prod: dict) -> dict | None:
    prod_type = str(prod.get("prod_type") or "")
    # On ne conserve que les maisons / propriétés (house*)
    base_type = prod_type.split("|")[0]
    if base_type not in _KEEP_TYPES:
        return None

    cp = str(prod.get("cp") or "").strip()
    ville = str(prod.get("city") or "").strip()

    titre = _localized(prod.get("title")) or str(prod.get("title_auto") or "")
    title_auto = str(prod.get("title_auto") or "")

    # Description : details{fr} (longue) en priorité, sinon meta_desc{fr}
    description = _localized(prod.get("details")) or _localized(prod.get("meta_desc"))

    # Prix : pricePrimary (calculé), repli price2 / price1
    prix = (
        _to_float(prod.get("pricePrimary"))
        or _to_float(prod.get("price2"))
        or _to_float(prod.get("price1"))
    )

    surface = _to_float(prod.get("surface"))
    surface_terrain = _to_float(prod.get("land"))

    # Pièces : longueur de roomsList, repli depuis title_auto
    rooms = prod.get("roomsList")
    pieces = len(rooms) if isinstance(rooms, list) and rooms else None
    if not pieces:
        pieces = _pieces_from_title_auto(title_auto)

    # Photos
    photos = [p for p in (prod.get("photos") or []) if isinstance(p, str) and p.startswith("http")]
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "karacterre",
        "url": _detail_url(prod),
        "id_annonce": str(prod.get("prod_ref") or ref),
        "titre": titre[:150],
        "type_bien": _TYPE_LABEL.get(base_type, "maison"),
        "description": (description or "")[:1200],
        "departement": cp[:2] if cp else "",
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Karacterre Immobilier",
    }


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen: set[str] = set()
    per_dept: dict[str, int] = {}

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{LISTING_URL}?page={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Karacterre] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            td = _b64_template_data(r.text)
            if not td:
                print(f"[Karacterre] page {page}: _TEMPLATE_DATA introuvable")
                break

            prod_id = td.get("prodId") or {}
            refs = (td.get("prodResults") or {}).get("search") or []
            if not refs:
                break

            new_on_page = 0
            for ref in refs:
                prod = prod_id.get(ref)
                if not isinstance(prod, dict):
                    continue
                try:
                    bien = _parse_product(ref, prod)
                except Exception:
                    continue
                if not bien:
                    continue

                # POST-FILTRE DÉPARTEMENT STRICT (0 fuite hors-zone)
                if not bien["code_postal"] or bien["code_postal"][:2] not in departements:
                    continue

                aid = bien["id_annonce"]
                if aid in seen:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                seen.add(aid)
                results.append(bien)
                per_dept[bien["departement"]] = per_dept.get(bien["departement"], 0) + 1
                new_on_page += 1

            # Page sans bien cible retenu : on continue (les depts cibles peuvent
            # apparaître plus loin dans la pagination) sauf si plus aucun ref.
            await asyncio.sleep(0.5)

    for dept in sorted(per_dept):
        print(f"[Karacterre] Dept {dept}: {per_dept[dept]} annonces")

    return results


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
    print(f"\nTotal Karacterre: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

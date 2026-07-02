"""scrapers/groupe_123_immo.py — Groupe 123 Immobilier (réseau 5 agences Yonne)

Méthode : scrape_simple (httpx) — SSR HTML (Tailwind, contenu dans le HTML brut).
Site : https://www.groupe123immo.com

URL pattern : /vente/{type}/{page}   (ex: /vente/maison/1, /vente/appartement/2)
              ou /vente/{page} pour tous types confondus.
  ⚠ Le slug département (/vente/89-yonne/1) renvoie 0 annonce (offerCount=0) :
    le 1er segment des URLs détail est un ID *ville* ({id}-{ville}), PAS un code
    département. Il n'existe AUCUN filtre département serveur fonctionnel.
    → On scrape par type (national en théorie) et on POST-FILTRE strictement
      sur code_postal[:2]. Catalogue réel : ~99% Yonne (89) + quelques biens
      limitrophes (Aube 10 vus). 0 fuite garantie par le post-filtre.

Cartes : div.item__block
  - Loc    : .item__block--city   →  "Aillant-sur-Tholon (89110)"
  - Prix   : .item__price         →  "124 900 €"
  - Options: div.option .option__number  (pièces / surface m² / divers — ambigus,
             on n'extrait que la surface "NNN m²" de façon prudente)
  - URL    : a[href] → /vente/{villeid-ville}/{type}/{listingid-slug}
  - Type   : déduit du segment d'URL ({type}) — on ne garde que maisons/propriétés.

Couverture cible (criteria.md 72/28/45/89) : seul 89 (Yonne) a du stock (~169
maisons). 72/28/45 = 0 (réseau mono-département). Les biens hors 72/28/45/89
(ex. Aube 10) sont écartés par le post-filtre.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.groupe123immo.com"
MAX_PAGES = 12
PHOTOS_PER_CARD = 10

# Types (segment d'URL /vente/{type}/{page}) à parcourir pour les maisons/propriétés.
SEARCH_TYPES = ["maison", "autre"]


# Types de bien (segment d'URL) à conserver : maisons / propriétés / fermes...
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|autre",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)
    dept_set = set(departements)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for type_slug in SEARCH_TYPES:
            try:
                biens = await _scrape_type(
                    client, type_slug, dept_set, prix_max, prix_min,
                    surface_min, seen_ids,
                )
                results.extend(biens)
                print(f"[Groupe123] Type '{type_slug}': {len(biens)} annonces retenues")
            except Exception as e:
                print(f"[Groupe123] Erreur type '{type_slug}': {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_type(
    client: httpx.AsyncClient,
    type_slug: str,
    dept_set: set[str],
    prix_max: int,
    prix_min: int,
    surface_min: int,
    seen_ids: set[str],
) -> list[dict]:
    biens: list[dict] = []

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/vente/{type_slug}/{page}"
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select("div.item__block")
        if not cards:
            break

        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue

            # POST-FILTRE STRICT : pas de filtre serveur → on ne garde que les
            # départements cibles (0 fuite).
            cp = bien["code_postal"]
            if not cp or cp[:2] not in dept_set:
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
            biens.append(bien)

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card) -> dict | None:
    link = card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href or "/vente/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Segments : /vente/{villeid-ville}/{type}/{listingid-slug}
    parts = [p for p in href.split("/") if p]
    # type = avant-dernier segment
    type_seg = parts[-2] if len(parts) >= 2 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        return None
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # id annonce = id numérique du dernier segment ({listingid}-{slug})
    id_annonce = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_annonce = m.group(1)
    id_annonce = id_annonce or url

    # Localisation : "Aillant-sur-Tholon (89110)"
    city_el = card.select_one(".item__block--city") or card.select_one(".title-v1")
    loc = city_el.get_text(" ", strip=True) if city_el else ""
    ville, code_postal = _parse_loc(loc)

    # Titre : depuis le slug du dernier segment (le HTML carte n'a pas de titre texte)
    slug = re.sub(r"^\d+-", "", parts[-1]) if parts else ""
    titre = slug.replace("-", " ").strip().capitalize()
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Prix
    price_el = card.select_one(".item__price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Options : tokens ambigus (icônes, sans label). Le "NNN m²" de la carte
    # correspond à la surface du TERRAIN (la surface habitable n'est que sur la
    # page détail). On le range donc en surface_terrain, et on laisse surface=None
    # pour ne pas fausser le filtre surface_min.
    opts_text = ""
    opts_el = card.select_one(".item__options")
    if opts_el:
        opts_text = opts_el.get_text(" ", strip=True)
    surface_terrain = _parse_m2(opts_text)
    surface = None

    # Photos
    photos = []
    for img in card.select("img"):
        src = img.get("data-src") or img.get("data-lazy") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "groupe_123_immo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Groupe 123 Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Aillant-sur-Tholon (89110)' → ('Aillant-sur-Tholon', '89110')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_m2(text: str) -> float | None:
    """Extrait le 1er nombre suivi de 'm²' dans les options (terrain)."""
    if not text:
        return None
    m = re.search(r"([\d][\d\s\xa0]*)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 1 <= f <= 1_000_000:
                return f
        except ValueError:
            pass
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
    print(f"\nTotal Groupe 123 Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

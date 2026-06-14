"""scrapers/immobilierpassion49.py — Immobilier Passion (agence indépendante, Angers 49)

Méthode : scrape_simple (httpx) — SSR HTML
Site : https://www.immobilierpassion49.com
URL pattern : /acheter.html  (page unique listant TOUS les biens à vendre — petite
              agence familiale mono-département, ~22 annonces). Pas de slug département
              ni de pagination ; le filtre département se fait CÔTÉ CLIENT.
Cartes : a[data-item="bien"] (CMS « data-db-items »).
  - titre      : .name
  - localité   : .state  → ex. « Angers (49) » → on extrait le code département entre ()
  - surface/terrain/pièces/chambres : spans de .datas (m², m², N pièces, N chambres)
  - prix       : .price .now
  - url        : href relatif acheter-{id}-{slug}.html
Filtre département : l'agence n'opère qu'en Maine-et-Loire (49). On ne renvoie un bien
  que si 49 ∈ departements demandés, ET on re-vérifie le « (49) » de chaque carte
  → 0 fuite hors-49 par construction.
Particularité : la vue liste ne donne pas le code postal complet, seulement le code
  département (49). On renseigne donc `departement` mais `code_postal=None`
  (gallery.py / geolocate.py compléteront en page détail si besoin).

Interface : async def search(criteres: dict) -> list[dict]
"""
from __future__ import annotations

import re

from scrapers._base import (
    get_with_retry,
    make_client,
    parse_int,
    parse_price,
)

BASE_URL = "https://www.immobilierpassion49.com"
LISTING_URL = f"{BASE_URL}/acheter.html"
DEPT = "49"

# Types à conserver (maison / longère / propriété…) ; on écarte appart/terrain/parking.
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|longere|longère|manoir|chateau|château|demeure|ferme|"
    r"corps de ferme|bourg|villa|moulin|gite|gîte|chai",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|appart|studio|terrain|parking|box|local|bureau|immeuble|duplex",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    if DEPT not in departements:
        print(f"[ImmoPassion49] Dept {DEPT} hors cible — 0 annonce")
        return []

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen: set[str] = set()

    async with make_client() as client:
        r = await get_with_retry(client, LISTING_URL)
        if r is None or r.status_code != 200:
            print(f"[ImmoPassion49] Listing inaccessible (status "
                  f"{getattr(r, 'status_code', 'None')})")
            return []

        from bs4 import BeautifulSoup
        cards = BeautifulSoup(r.text, "html.parser").select("a[data-item='bien']")
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue
            # Garde-fou département : le bien doit être en 49.
            if bien["departement"] != DEPT:
                continue
            aid = bien.get("id_annonce") or bien.get("url")
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

    print(f"[ImmoPassion49] Dept {DEPT}: {len(results)} annonces")
    return results


def _parse_card(card) -> dict | None:
    href = card.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

    name_el = card.select_one(".name")
    titre = name_el.get_text(" ", strip=True) if name_el else ""
    if not titre:
        return None

    # Filtre type : on ne garde que maison/propriété/longère…
    if _EXCLUDE_TYPE.search(titre) and not _KEEP_TYPE.search(titre):
        return None
    if not _KEEP_TYPE.search(titre):
        return None

    # Localité : « Angers (49) » → ville + code département.
    state_el = card.select_one(".state")
    state = state_el.get_text(" ", strip=True) if state_el else ""
    m_dep = re.search(r"\((\d{2})\)", state)
    dept = m_dep.group(1) if m_dep else ""
    ville = re.sub(r"\s*\(\d{2}\)\s*$", "", state).strip().title()

    # id_annonce : numéro dans le slug acheter-{ID}-...
    m_id = re.search(r"acheter-(\d+)-", href)
    id_annonce = m_id.group(1) if m_id else url

    # Données chiffrées : spans de .datas — « 190 m² », « 370 m² », « 8 pièces »,
    # « 5 chambres ». On classe par sémantique plutôt que par position (un bien sans
    # terrain décale l'ordre).
    surface = surface_terrain = None
    pieces = chambres = None
    surfaces_m2: list[float] = []
    datas = card.select_one(".datas")
    if datas:
        for span in datas.select("span"):
            txt = span.get_text(" ", strip=True)
            low = txt.lower()
            if "pièce" in low or "piece" in low:
                pieces = parse_int(r"(\d+)", txt)
            elif "chambre" in low:
                chambres = parse_int(r"(\d+)", txt)
            elif "m²" in txt or "m2" in low:
                val = re.sub(r"[^\d]", "", txt)
                if val:
                    surfaces_m2.append(float(val))
    # 1ère surface = habitable, 2ᵉ = terrain (convention du gabarit du site).
    if surfaces_m2:
        surface = surfaces_m2[0]
        if len(surfaces_m2) > 1:
            surface_terrain = max(surfaces_m2[1:])

    price_el = card.select_one(".price .now")
    prix = parse_price(price_el.get_text(" ", strip=True)) if price_el else None

    photos: list[str] = []
    img = card.select_one("img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if not src.startswith("http"):
                src = f"{BASE_URL}/{src.lstrip('/')}"
            photos.append(src)

    return {
        "source": "immobilierpassion49",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": None,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Immobilier Passion",
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Immobilier Passion 49")

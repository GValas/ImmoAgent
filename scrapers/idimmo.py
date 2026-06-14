"""scrapers/idimmo.py — IDIMMO (réseau de mandataires / centrale immobilière)

Méthode : scrape_simple (httpx) — SSR HTML (CMS « La Boîte Immo », même gabarit
          que Le Tuc).
URL pattern : /vente/{NN-dept-slug}/{page}   (ex: /vente/45-loiret/1)
              → filtre département CÔTÉ SERVEUR. Vérifié sur 45/18/36 : aucune
              fuite hors-département (chaque carte porte son code postal dans
              .title__subtitle, re-vérifié par le post-filtre strict CP[:2]).
Cartes : article.property-v3
Champs carte :
  - .title__subtitle      → "Ville (CP)"
  - .title__content       → titre
  - .property-v3__text    → description
  - .property-v3__price-value / .property-v3__price → prix
  - .property-v3__reference-number → référence
  - segment d'URL /…/1-maison/t6/…  → type de bien + nb de pièces
  - img[data-src]         → photos (lazy-load, placeholder SVG en src)

Pas de surface ni de terrain en vue liste (récupérés ensuite en page détail par
gallery.py). Le type est dérivé du segment d'URL ; les types non résidentiels
(appartement/terrain/commerce/immeuble…) sont écartés.

Interface : async def search(criteres: dict) -> list[dict]
"""
import re

from scrapers._base import (
    parse_int,
    parse_loc,
    parse_price,
    parse_surface,
    run_dept_search,
    standalone_main,
)

BASE_URL = "https://www.idimmo.net"
PHOTOS_PER_CARD = 10

_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|maison-de-village",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    return await run_dept_search(
        source="idimmo",
        label="IDIMMO",
        page_url=lambda dept, slug, page: f"{BASE_URL}/vente/{dept}-{slug}/{page}",
        card_selector="article.property-v3",
        parse_card=_parse_card,
        criteres=criteres,
    )


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a.property-v3__link") or card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien + pièces depuis le segment d'URL :
    #   /vente/45-loiret/1645-st-loup-des-vignes/1-maison/t6/17927-titre-slug/
    parts = [p for p in href.split("/") if p]
    type_seg = ""
    pieces = None
    for i, p in enumerate(parts):
        m = re.match(r"^\d+-(maison|appartement|villa|propriete|propriété|ferme|"
                     r"longere|longère|manoir|chateau|château|moulin|demeure|"
                     r"domaine|mas|terrain|local|commerce|immeuble|bureau|fonds|"
                     r"gite|gîte|corps-de-ferme|maison-de-village)$", p, re.IGNORECASE)
        if m:
            type_seg = m.group(1)
            # pièces dans le segment suivant (tN)
            if i + 1 < len(parts):
                mp = re.match(r"^t(\d+)$", parts[i + 1])
                if mp:
                    pieces = int(mp.group(1))
            break
    if type_seg:
        if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
            return None
        if not _KEEP_TYPE.search(type_seg):
            return None
    type_bien = (type_seg or "maison").lower()

    sub_el = card.select_one(".title__subtitle")
    ville, code_postal = parse_loc(sub_el.get_text(" ", strip=True) if sub_el else "")

    title_el = card.select_one(".title__content")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    text_el = card.select_one(".property-v3__text")
    description = text_el.get_text(" ", strip=True) if text_el else ""

    ref_el = card.select_one(".property-v3__reference-number")
    ref = ref_el.get_text(strip=True) if ref_el else ""
    id_num = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_num = m.group(1)
    id_annonce = ref or id_num or url

    price_el = card.select_one(".property-v3__price-value") or card.select_one(
        ".property-v3__price"
    )
    prix = parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    if pieces is None:
        pieces = parse_int(r"(\d+)\s*pi[eè]ces", titre + " " + description)

    surface = parse_surface(titre) or parse_surface(description)

    photos = []
    for img in card.select("img"):
        src = img.get("data-src") or img.get("data-lazy") or ""
        if not src and img.get("src") and not img.get("src", "").startswith("data:"):
            src = img.get("src")
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "idimmo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "IDIMMO",
    }


if __name__ == "__main__":
    standalone_main(search, "IDIMMO")

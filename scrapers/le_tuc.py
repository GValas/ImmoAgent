"""scrapers/le_tuc.py — Le Tuc Immo (réseau d'agences / mandataires)

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /vente/{NN-dept-slug}/{page}   (ex: /vente/45-loiret/1)
              → filtre département CÔTÉ SERVEUR (vérifié : aucune fuite hors-dept).
Cartes : article.property

Pilote de migration vers scrapers/_base.py : HEADERS, map dept→slug, boucle
département + pagination, filtres prix/surface, dédup et helpers de parsing sont
désormais fournis par le socle. Ce fichier ne porte plus que ce qui est PROPRE à
Le Tuc : le patron d'URL, le sélecteur de carte et le mapping des champs.

Interface : async def search(criteres: dict) -> list[dict]
"""
import re

from scrapers._base import (
    parse_int,
    parse_loc,
    parse_price,
    parse_surface,
    parse_terrain,
    run_dept_search,
    standalone_main,
)

BASE_URL = "https://www.letuc.com"
PHOTOS_PER_CARD = 10

# Types de bien (segment d'URL) à conserver : maisons / propriétés / fermes...
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
        source="le_tuc",
        label="LeTuc",
        page_url=lambda dept, slug, page: f"{BASE_URL}/vente/{dept}-{slug}/{page}",
        card_selector="article.property",
        parse_card=_parse_card,
        criteres=criteres,
    )


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a.property__link")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL : /vente/45-loiret/{ville}/{type}/{tN}/{id-slug}/
    parts = [p for p in href.split("/") if p]
    type_seg = parts[3] if len(parts) > 3 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean) and not _KEEP_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        return None  # type inconnu/ambigu → on exclut par prudence
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # Référence (id_annonce) : .property__reference-number, sinon id numérique du slug
    ref_el = card.select_one(".property__reference-number")
    ref = ref_el.get_text(strip=True) if ref_el else ""
    id_num = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_num = m.group(1)
    id_annonce = ref or id_num or url

    sub_el = card.select_one(".title__subtitle")
    ville, code_postal = parse_loc(sub_el.get_text(" ", strip=True) if sub_el else "")

    title_el = card.select_one(".title__content")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    text_el = card.select_one(".property__text")
    description = text_el.get_text(" ", strip=True) if text_el else ""

    price_el = card.select_one(".property__price")
    prix = parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    opts_el = card.select_one(".property__options")
    opts_text = opts_el.get_text(" ", strip=True) if opts_el else ""
    pieces = parse_int(r"Nombre de pi[eè]ces\s*(\d+)", opts_text)
    chambres = parse_int(r"Nombre de chambres\s*(\d+)", opts_text)
    surface_terrain = parse_terrain(opts_text)

    surface = parse_surface(titre) or parse_surface(description)

    # Pièces en secours : segment tN de l'URL
    if pieces is None and len(parts) > 4:
        m = re.match(r"^t(\d+)$", parts[4])
        if m:
            pieces = int(m.group(1))

    photos = []
    for img in card.select(".property__img"):
        src = img.get("data-src") or img.get("data-lazy") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "le_tuc",
        "url": url,
        "id_annonce": id_annonce,
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
        "dpe": None,
        "agence": "Le Tuc Immo",
    }


if __name__ == "__main__":
    standalone_main(search, "Le Tuc")

"""scrapers/trovit_immo.py — Trovit Immobilier (immo.trovit.fr, agrégateur LeBonCoin/
Bien'ici/LuxuryEstate…)

Méthode : scrape_simple (httpx) — SSR HTML.
URL pattern : /maison-departement-{slug}[/{page}] (page 1 sans suffixe, ~30 cartes/page).
ATTENTION filtre : la recherche est TEXTUELLE, pas géographique. Le préfixe
« departement- » rend le geo-matching quasi parfait (30/30 in-dept sur 9 slugs,
vs 40-60 % de fuites IDF avec « maison-loiret » nu, et 0 % de pertinence pour les
noms composés « indre-et-loire » → slug Trovit SANS « et » : indre-loire).
Reste des résidus (loir-cher mélange 41+28) → post-filtre CP STRICT (keep_bien)
obligatoire ; on écarte aussi les cartes data-is-similar="true".
Cartes : article.snippet-listing[data-id] — a.js-listing[title], .price__actual
         (« Consulter le prix » → prix None), .address_property-type
         (« Maison à 45340, Ville, Loiret, Centre-Val de Loire »), icônes
         pièces/salle de bain/surface, description <p>, éditeur (LEBONCOIN…).
URL annonce : page interstitielle Trovit /detail/{id} extraite du paramètre
              detailPageUrl du lien de tracking clk.thribee.com.
Requêtes SANS header Accept → « Access Denied » (le make_client du socle l'envoie).

Interface : async def search(criteres: dict) -> list[dict]
"""
import re
from urllib.parse import parse_qs, unquote, urlparse

from scrapers._base import (
    parse_int,
    parse_price_digits,
    run_dept_search,
    standalone_main,
)

BASE_URL = "https://immo.trovit.fr"
PHOTOS_PER_CARD = 3

# Slugs Trovit (recherche TEXTUELLE) — map mixte mesurée le 2026-07-04 :
#  - nom nu quand il est distinctif (sarthe 37 kept vs 5 en prefixé, mayenne 11 vs 5…) ;
#  - « departement-{nom} » (noms composés SANS « et ») quand le nom nu draine du
#    bruit IDF ou du hors-sujet (« maison cher » = maison chère !, indre → 92/78,
#    maine-et-loire/indre-et-loire → 0 % in-dept en nu).
DEPT_SLUGS = {
    "72": "sarthe",
    "28": "eure-et-loir",
    "45": "loiret",
    "89": "yonne",
    "49": "departement-maine-loire",
    "37": "departement-indre-loire",
    "36": "departement-indre",
    "18": "departement-cher",
    "58": "departement-nievre",
    "41": "loir-et-cher",
    "53": "mayenne",
}

_RE_CP_VILLE = re.compile(r"\b(\d{5})\b[,\s]*([^,]*)")
_RE_SURFACE = re.compile(r"(\d[\d\s\xa0]*)\s*m²")
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    return await run_dept_search(
        source="trovit_immo",
        label="TrovitImmo",
        page_url=lambda dept, slug, page: (
            f"{BASE_URL}/maison-{slug}" + ("" if page == 1 else f"/{page}")
        ),
        card_selector="article.snippet-listing",
        parse_card=_parse_card,
        criteres=criteres,
        dept_slugs=DEPT_SLUGS,
    )


def _parse_card(card, dept: str) -> dict | None:
    if card.get("data-is-similar") == "true":
        return None  # suggestions « similaires » hors recherche
    data_id = card.get("data-id") or ""
    if not data_id:
        return None

    link = card.select_one("a.js-listing")
    href = link.get("href", "") if link else ""
    # Lien de tracking thribee → on extrait la page interstitielle Trovit
    url = f"{BASE_URL}/detail/{data_id}"
    if "detailPageUrl=" in href:
        try:
            qs = parse_qs(urlparse(href).query)
            detail = unquote(qs.get("detailPageUrl", [""])[0])
            if detail.startswith("http"):
                url = detail.split("?")[0]
        except Exception:
            pass

    titre = (link.get("title") or "").strip() if link else ""

    addr_el = card.select_one(".address_property-type")
    addr = addr_el.get_text(" ", strip=True) if addr_el else ""
    m_loc = _RE_CP_VILLE.search(addr)
    code_postal = m_loc.group(1) if m_loc else None
    ville = (m_loc.group(2) or "").strip() if m_loc else ""

    type_el = addr_el.select_one("b") if addr_el else None
    type_bien = (type_el.get_text(strip=True) if type_el else "maison").lower()
    if _EXCLUDE_TYPE.search(type_bien):
        return None

    prix_el = card.select_one(".price__actual")
    prix = parse_price_digits(prix_el.get_text(strip=True) if prix_el else "")
    # « Consulter le prix » → prix None (gardé : le filtre structurel tranchera)

    icons_el = card.select_one(".snippet-listing-content-header-icons")
    icons_text = icons_el.get_text(" ", strip=True) if icons_el else ""
    pieces = parse_int(r"(\d+)\s*pi[eè]ces?", icons_text)
    chambres = parse_int(r"(\d+)\s*chambres?", icons_text)
    surface = None
    m_s = _RE_SURFACE.search(icons_text)
    if m_s:
        try:
            val = float(re.sub(r"[\s\xa0]", "", m_s.group(1)))
            if 8 <= val <= 2000:
                surface = val
        except ValueError:
            pass

    desc_el = card.select_one(".snippet-listing-content-header-description p")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    pub_el = card.select_one(".date-publisher-wrapper-agency small")
    agence = pub_el.get_text(strip=True)[:80] if pub_el else None

    photos = []
    for img in card.select(".snippet-listing-image img"):
        src = img.get("data-src") or img.get("src") or ""
        if src.startswith("http") and "images.trovit" in src:
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "trovit_immo",
        "url": url,
        "id_annonce": data_id,
        "titre": titre[:150] or f"Maison {ville}".strip(),
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": agence,
    }


if __name__ == "__main__":
    standalone_main(search, "Trovit Immo")

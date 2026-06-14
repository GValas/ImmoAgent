"""scrapers/destindepierre.py — Destin de Pierre (biens de caractère, Brive / Corrèze 19)

Méthode : scrape_simple (httpx) — SSR HTML (template Wizi-v1 « item__info »).
URL pattern : /vente/{NN-dept-slug}/{page}   (ex: /vente/36-indre/1)
              → FILTRE DÉPARTEMENT CÔTÉ SERVEUR fiable (vérifié : /vente/36-indre/1
              renvoie 0 carte quand l'agence n'a pas de stock dans le 36).
Cartes : div.item__info
  .title-subtitle__subtitle  → « Ville (CP) »
  .title-subtitle__content   → titre
  .item__info-id             → « Réf : NNNN »
  .item__info-extra → « NNN m² » + .__price-value « NNN NNN € »
  a.item__link href /vente/{NN-dept}/{city}/{type}/t{N}/{id}-{slug}/ → pièces depuis tN

Particularité : agence de prestige / pierre de caractère du bassin de Brive
(Corrèze 19, hors zone cible actuelle). Scraper conservé pour le segment Sud-Ouest /
Limousin : il interroge directement les départements cibles via le slug et
post-filtre code_postal[:2] → 0 fuite. dernier_test : 0 stock dans la zone (36/18…).

Interface : async def search(criteres: dict) -> list[dict]
"""
import re

from scrapers._base import parse_price, parse_surface, run_dept_search, standalone_main

BASE_URL = "https://www.destindepierre.fr"
SOURCE = "destindepierre"

# Slugs « NN-nom-departement » des départements cibles (le site les accepte tels quels).
DEPT_SLUGS = {
    "72": "72-sarthe", "28": "28-eure-et-loir", "45": "45-loiret",
    "89": "89-yonne", "49": "49-maine-et-loire", "37": "37-indre-et-loire",
    "36": "36-indre", "18": "18-cher", "58": "58-nievre",
    "41": "41-loir-et-cher", "53": "53-mayenne",
}

_EXCLUDE = re.compile(r"appartement|terrain|immeuble|local|commerce|garage|parking|fonds", re.IGNORECASE)


async def search(criteres: dict) -> list[dict]:
    return await run_dept_search(
        source=SOURCE,
        label="DestinDePierre",
        page_url=lambda dept, slug, page: f"{BASE_URL}/vente/{slug}/{page}",
        card_selector="div.item__info",
        parse_card=_parse_card,
        criteres=criteres,
        dept_slugs=DEPT_SLUGS,
        max_pages=10,
    )


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a.item__link") or card.find("a", href=re.compile(r"^/vente/\d+-"))
    if not link:
        return None
    href = link.get("href", "")
    if _EXCLUDE.search(href):
        return None
    url = href if href.startswith("http") else BASE_URL + href

    sub = card.select_one(".title-subtitle__subtitle")
    ville, cp = _parse_loc(sub.get_text(" ", strip=True) if sub else "")

    title_el = card.select_one(".title-subtitle__content")
    titre = title_el.get_text(" ", strip=True) if title_el else f"Bien {ville}".strip()

    ref_el = card.select_one(".item__info-id")
    ref = ""
    if ref_el:
        m = re.search(r"R[ée]f\.?\s*:?\s*([0-9A-Za-z]+)", ref_el.get_text(" ", strip=True))
        if m:
            ref = m.group(1)
    id_annonce = ref or url

    price_el = card.select_one(".__price-value")
    prix = parse_price(price_el.get_text(" ", strip=True)) if price_el else None

    # Surface : « NNN m² » dans les blocs .item__info-extra (hors prix)
    surface = None
    for ex in card.select(".item__info-extra"):
        t = ex.get_text(" ", strip=True)
        if "€" in t:
            continue
        m = re.search(r"([\d\s\xa0]+)\s*m²", t)
        if m:
            try:
                surface = float(re.sub(r"[\s\xa0]", "", m.group(1)))
                break
            except ValueError:
                pass
    if surface is None:
        surface = parse_surface(titre)

    pieces = None
    m_t = re.search(r"/t(\d+)/", href)
    if m_t:
        pieces = int(m_t.group(1))

    photos = []
    img = card.find("img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)

    return {
        "source": SOURCE,
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Destin de Pierre",
    }


def _parse_loc(text: str) -> tuple[str, str]:
    cp = ""
    m = re.search(r"\((\d{5})\)", text or "")
    if m:
        cp = m.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text or "").strip()
    return ville, cp


if __name__ == "__main__":
    standalone_main(search, "Destin de Pierre")

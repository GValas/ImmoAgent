"""scrapers/faureimmo.py — Faure Immo (réseau 6 agences, bassin de Brive / Corrèze 19)

Méthode : scrape_simple (httpx) — SSR HTML (template Wizi-v3 « property-v3 »).
URL pattern : /vente/{NN-dept-slug}/{page}   (ex: /vente/36-indre/1)
              → FILTRE DÉPARTEMENT CÔTÉ SERVEUR (vérifié : /vente/36-indre/1 → 0
              carte ; /vente/19-correze/1 → 10 cartes).
Cartes : div.property-v3__content-wrapper
  .title__subtitle             → « Ville (CP) »
  .title__content              → titre
  .property-v3__text           → description complète
  .property-v3__reference-number → référence
  .__price-value               → prix
  a.property-v3__link href /vente/{NN-dept}/{city}/{type}/t{N}/{id}-{slug}/ → pièces

Particularité : réseau Faure Immo (Brive-la-Gaillarde, Objat, Larche, Donzenac…),
Corrèze 19 — hors zone cible actuelle. Scraper du segment Sud-Ouest/Limousin :
interroge directement les départements cibles via le slug, post-filtre code_postal[:2]
→ 0 fuite. dernier_test : 0 stock dans la zone (36/18/37/41…).

Interface : async def search(criteres: dict) -> list[dict]
"""
import re

from scrapers._base import parse_price, parse_surface, run_dept_search, standalone_main

BASE_URL = "https://www.faureimmo.fr"
SOURCE = "faureimmo"

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
        label="FaureImmo",
        page_url=lambda dept, slug, page: f"{BASE_URL}/vente/{slug}/{page}",
        card_selector="div.property-v3__content-wrapper",
        parse_card=_parse_card,
        criteres=criteres,
        dept_slugs=DEPT_SLUGS,
        max_pages=12,
    )


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a.property-v3__link") or card.find("a", href=re.compile(r"^/vente/\d+-"))
    if not link:
        return None
    href = link.get("href", "")
    if _EXCLUDE.search(href):
        return None
    url = href if href.startswith("http") else BASE_URL + href

    sub = card.select_one(".title__subtitle")
    ville, cp = _parse_loc(sub.get_text(" ", strip=True) if sub else "")

    title_el = card.select_one(".title__content")
    titre = title_el.get_text(" ", strip=True) if title_el else f"Maison {ville}".strip()

    desc_el = card.select_one(".property-v3__text")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    ref_el = card.select_one(".property-v3__reference-number")
    ref = ref_el.get_text(strip=True) if ref_el else ""
    id_annonce = ref or url

    price_el = card.select_one(".__price-value") or card.select_one(".property-v3__price")
    prix = parse_price(price_el.get_text(" ", strip=True)) if price_el else None

    pieces = None
    m_t = re.search(r"/t(\d+)/", href)
    if m_t:
        pieces = int(m_t.group(1))

    surface = parse_surface(titre) or parse_surface(description)

    photos = []
    img = card.find("img")
    if img:
        src = img.get("data-src") or img.get("data-lazy") or img.get("src") or ""
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
        "description": description[:1200],
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
        "agence": "Faure Immo",
    }


def _parse_loc(text: str) -> tuple[str, str]:
    cp = ""
    m = re.search(r"\((\d{5})\)", text or "")
    if m:
        cp = m.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text or "").strip()
    return ville, cp


if __name__ == "__main__":
    standalone_main(search, "Faure Immo")

"""scrapers/agence_dilo.py — Agence Dilo (Saint-Florentin, nord Yonne 89 —
stock Saint-Florentin / Migennes / Brienon et alentours, quasi 100 % dept 89)

Méthode : scrape_simple (httpx) — SSR HTML (CMS « office »/Orisha, même famille
que lair_immobilier mais gabarit à sélecteurs pluriels `products-*`).
URL pattern : /annonces/transaction/Vente.html?page={N} (10 biens/page, ~4
pages ; le CMS re-sert les mêmes cartes au-delà) → POST-FILTRE STRICT
code_postal[:2] (le CP est SUR la carte : .products-localisation
"89600 Saint florentin" — CP en tête), 0 fuite.

Cartes : div.item-product-listing
  - URL    : a[href*='fiches'] (relatif "../fiches/{ids}_{id}/slug.html")
  - CP/Ville : .products-localisation → "89600 SAINT FLORENTIN"
  - Titre  : .products-name ; Prix : .products-price ; Desc : .products-description
  - Photo  : img.photo-listing (relatif "../office20/...")
Pas de pièces/chambres/terrain sur la carte (surface parfois dans titre/desc
via parse_surface) → le reste est laissé à gallery.py.

Types conservés : maisons / propriétés / pavillons / fermes… (terrains, locaux,
immeubles, fonds… exclus par mots-clés du titre).
Le scraper ne requête que si un département de son stock (89) est demandé.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import (
    get_with_retry,
    keep_bien,
    make_client,
    parse_price_digits,
    parse_surface,
    standalone_main,
)

BASE_URL = "https://www.agencedilo.fr"
LISTING_URL = f"{BASE_URL}/annonces/transaction/Vente.html"
SOURCE = "agence_dilo"
LABEL = "AgenceDilo"
AGENCE = "Agence Dilo"
DEPTS_STOCK = {"89"}
MAX_PAGES = 12
PHOTOS_PER_CARD = 10

_KEEP_TITLE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|fermette|long[èe]re|manoir|ch[âa]teau|"
    r"moulin|demeure|domaine|g[îi]te|corps de ferme|b[âa]tisse|grange|pavillon|"
    r"presbyt[èe]re|ensemble immobilier",
    re.IGNORECASE,
)
_EXCLUDE_TITLE = re.compile(
    r"appartement|\bterrain\b|local (commercial|professionnel|d'activit)|garage|"
    r"parking|immeuble|bureau|fonds de commerce|\bcave\b|\bbox\b|hangar|studio|"
    r"viager|entrep[ôo]t|b[âa]timent",
    re.IGNORECASE,
)


def _abs_url(href: str) -> str:
    if href.startswith("http"):
        return href
    return f"{BASE_URL}/{href.lstrip('./')}"


def _parse_card(card) -> dict | None:
    link = card.select_one("a[href*='fiches']")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = _abs_url(href)

    name_el = card.select_one(".products-name")
    titre = name_el.get_text(" ", strip=True) if name_el else ""

    # Type : uniquement résidentiel maison/propriété, décidé sur le titre.
    if _EXCLUDE_TITLE.search(titre) and not _KEEP_TITLE.search(titre):
        return None
    if not _KEEP_TITLE.search(titre):
        return None  # titre ambigu → exclu par prudence
    m_type = _KEEP_TITLE.search(titre)
    type_bien = m_type.group(0).lower() if m_type else "maison"

    # CP + ville : "89600 Saint florentin" (CP en tête).
    ville, code_postal = "", ""
    loc_el = card.select_one(".products-localisation")
    if loc_el:
        m = re.match(r"(\d{5})\s+(.*)", loc_el.get_text(" ", strip=True))
        if m:
            code_postal, ville = m.group(1), m.group(2).strip().title()

    price_el = card.select_one(".products-price")
    price_text = price_el.get_text(" ", strip=True) if price_el else ""
    # « 133 000 € dont 6.4% TTC d'honoraires » → couper avant les honoraires
    price_text = re.split(r"\bdont\b|\bhonoraires\b|%", price_text, flags=re.IGNORECASE)[0]
    prix = parse_price_digits(price_text)

    desc_el = card.select_one(".products-description")
    description = re.sub(
        r"\s+", " ", desc_el.get_text(" ", strip=True) if desc_el else ""
    ).strip()

    # Surface parfois dans le titre ("Maison 130 m2...") ou la description.
    surface = parse_surface(titre) or parse_surface(description)
    if surface is None:
        m = re.search(r"(\d{2,4})\s*m2", f"{titre} {description}", re.IGNORECASE)
        if m:
            val = float(m.group(1))
            if 8 <= val <= 2000:
                surface = val

    m_id = re.search(r"_(\d{5,})/", href)
    id_annonce = m_id.group(1) if m_id else url

    photos = []
    for img in card.select("img.photo-listing"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(_abs_url(src))
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": SOURCE,
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": AGENCE,
    }


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    if not DEPTS_STOCK.intersection(departements):
        return []  # agence locale : rien à chercher hors de ses départements

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()
    seen_card_ids: set[str] = set()

    async with make_client() as client:
        for page in range(1, MAX_PAGES + 1):
            r = await get_with_retry(client, f"{LISTING_URL}?page={page}")
            if r is None or r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select("div.item-product-listing")
            if not cards:
                break

            # Fin de listing : le CMS re-sert les mêmes cartes au-delà de la
            # dernière page → stop si aucune carte nouvelle.
            page_ids = []
            for card in cards:
                a = card.select_one("a[href*='fiches']")
                h = a.get("href", "") if a else ""
                m = re.search(r"_(\d{5,})/", h)
                page_ids.append(m.group(1) if m else h)
            if page > 1 and all(cid in seen_card_ids for cid in page_ids):
                break
            seen_card_ids.update(page_ids)

            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue
                cp = bien["code_postal"]
                # Post-filtre STRICT département → 0 fuite hors zone.
                if not cp or cp[:2] not in departements:
                    continue
                if not keep_bien(bien, cp[:2], seen_ids, prix_max=prix_max,
                                 prix_min=prix_min, surface_min=surface_min):
                    continue
                results.append(bien)

            await asyncio.sleep(0.5)

    par_dept: dict[str, int] = {}
    for b in results:
        par_dept[b["departement"]] = par_dept.get(b["departement"], 0) + 1
    for d, n in sorted(par_dept.items()):
        print(f"[{LABEL}] Dept {d}: {n} annonces")
    if not par_dept:
        print(f"[{LABEL}] 0 annonce retenue")

    return results


if __name__ == "__main__":
    standalone_main(search, AGENCE)

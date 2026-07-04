"""scrapers/lair_immobilier.py — Lair Immobilier (réseau familial Alençon/Orne 61,
avec agences en zone : Mamers + Villeneuve-en-Perseigne (72600), Fresnay-sur-
Sarthe (72130), Saint-Pierre-des-Nids & Pré-en-Pail (53), déborde sur le Perche 28)

Méthode : scrape_simple (httpx) — SSR HTML (même CMS "office" que blois_immo :
listing catalogue unique, recherche avancée PHP non joignable en GET direct).
URL pattern : /annonces/transaction/Vente.html?page={N} (18 biens/page, ~42
pages, catalogue multi-dept 61-majoritaire) → POST-FILTRE STRICT code_postal[:2]
(le CP est SUR la carte : .product-localisation "VILLE (61410)"), 0 fuite.

Cartes : div.listing-item
  - URL    : a.link-product[href]  (relatif "../fiches/{ids}_{id}/slug.html")
  - CP/Ville : .product-localisation  → "PRE EN PAIL SAINT SAMSON (53140)"
  - Titre  : .product-name ; Prix : .product-price (texte direct, hors sous-span
             "Honoraires inclus")
  - Bulles : .bulle .value → entiers (pièces puis chambres) + "NNN m²" (surface)
  - Réf    : .product-ref "Ref : O15003"
Le détail (terrain, DPE) n'est pas sur la carte → laissé à gallery.py.

Types conservés : maisons / propriétés / demeures (fonds de commerce, immeubles,
terrains, locaux, garages… exclus par mots-clés du titre).
Le scraper ne requête que si un département de son stock (72/53/28) est demandé.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import (
    get_with_retry,
    keep_bien,
    make_client,
    parse_loc,
    parse_price_digits,
    standalone_main,
)

BASE_URL = "https://www.lair-immobilier.com"
LISTING_URL = f"{BASE_URL}/annonces/transaction/Vente.html"
SOURCE = "lair_immobilier"
LABEL = "LairImmobilier"
AGENCE = "Lair Immobilier"
# Départements en zone où le réseau a du stock (le gros du catalogue est en 61).
DEPTS_STOCK = {"72", "53", "28"}
MAX_PAGES = 50
PHOTOS_PER_CARD = 10

_KEEP_TITLE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|fermette|long[èe]re|manoir|ch[âa]teau|"
    r"moulin|demeure|domaine|g[îi]te|corps de ferme|b[âa]tisse|grange|pavillon|"
    r"presbyt[èe]re|ensemble immobilier|ensemble\b",
    re.IGNORECASE,
)
_EXCLUDE_TITLE = re.compile(
    r"appartement|\bterrain\b|local (commercial|professionnel|d'activit)|garage|"
    r"parking|immeuble|bureau|fonds de commerce|\bcave\b|\bbox\b|hangar|studio|"
    r"viager|entrep[ôo]t|galerie marchande|b[âa]timent",
    re.IGNORECASE,
)


def _abs_url(href: str) -> str:
    if href.startswith("http"):
        return href
    return f"{BASE_URL}/{href.lstrip('./')}"


def _parse_card(card) -> dict | None:
    link = card.select_one("a.link-product")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = _abs_url(href)

    loc_el = card.select_one(".product-localisation")
    ville, code_postal = parse_loc(loc_el.get_text(" ", strip=True) if loc_el else "")

    name_el = card.select_one(".product-name")
    titre = name_el.get_text(" ", strip=True) if name_el else ""

    # Type : uniquement résidentiel maison/propriété, décidé sur le titre.
    if _EXCLUDE_TITLE.search(titre) and not _KEEP_TITLE.search(titre):
        return None
    if not _KEEP_TITLE.search(titre):
        return None  # titre ambigu → exclu par prudence
    m_type = _KEEP_TITLE.search(titre)
    type_bien = m_type.group(0).lower() if m_type else "maison"

    # Prix : texte direct de .product-price (le sous-span "Honoraires inclus"
    # ne contient pas de chiffres, mais on l'écarte quand même par prudence).
    prix = None
    price_el = card.select_one(".product-price")
    if price_el:
        raw = "".join(t for t in price_el.find_all(string=True, recursive=False))
        if not raw.strip():
            raw = re.split(r"\bdont\b|Honoraires", price_el.get_text(" ", strip=True))[0]
        prix = parse_price_digits(raw)

    # Bulles : entiers = pièces puis chambres ; valeur en "m²" = surface.
    pieces = chambres = None
    surface = None
    ints: list[int] = []
    for val in card.select(".bulle .value"):
        txt = val.get_text(" ", strip=True)
        if re.search(r"m", txt):
            m = re.search(r"([\d]+(?:[.,]\d+)?)", txt)
            if m:
                surface = float(m.group(1).replace(",", "."))
        else:
            m = re.search(r"\d+", txt)
            if m:
                ints.append(int(m.group(0)))
    if ints:
        pieces = ints[0]
        if len(ints) > 1:
            chambres = ints[1]

    ref_el = card.select_one(".product-ref")
    ref = ""
    if ref_el:
        m = re.search(r"Ref\s*:?\s*(\S+)", ref_el.get_text(" ", strip=True), re.IGNORECASE)
        if m:
            ref = m.group(1)
    m_id = re.search(r"_(\d{5,})/", href)
    id_annonce = ref or (m_id.group(1) if m_id else url)

    photos = []
    for img in card.select("img.photo"):
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
        "description": "",
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": AGENCE,
    }


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    if not DEPTS_STOCK.intersection(departements):
        return []  # réseau local : rien à chercher hors de ses départements

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
            cards = soup.select("div.listing-item")
            if not cards:
                break

            # Fin de listing : le CMS re-sert les mêmes cartes au-delà de la
            # dernière page → stop si aucune carte nouvelle.
            page_ids = []
            for card in cards:
                a = card.select_one("a.link-product")
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

"""scrapers/hersant_immobilier.py — Hersant Immobilier (3 agences : Meung-sur-
Loire / Beaugency (45) et Saint-Laurent-Nouan (41), ouest orléanais + nord 41)

Méthode : scrape_simple (httpx) — SSR HTML (CMS La Boîte Immo « LBI », images
*.staticlbi.com — même famille qu'idimmo). Listing catalogue unique paginé.
URL pattern : /vente/{page} (12 biens/page) → POST-FILTRE STRICT
code_postal[:2] (le CP est SUR la carte : .title__content-1 "Baccon (45130)"),
0 fuite.

Cartes : article.property-listing-v2__container (classe .item)
  - URL/type/pièces : a.item__title[href] "/vente/{id-ville}/{type}/t{N}/{id-slug}/"
  - Ville/CP : .title__content-1 "Baccon (45130)"
  - Prix     : .item__price .__price-value
  - Extrait  : .item__text-block (surface via "NNN m² habitables")
  - Réf      : .item__reference "Réf : 8024" (id numérique du slug en secours)
  - Photo    : img.decorate__img (une seule en liste ; galerie via gallery.py)

Types conservés : segment d'URL maison/propriété/longère… (appartement, terrain,
immeuble, local… exclus). Le scraper ne requête que si un département de son
stock (45/41) est demandé.

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
    parse_surface,
    standalone_main,
)

BASE_URL = "https://www.hersantimmo.com"
SOURCE = "hersant_immobilier"
LABEL = "HersantImmo"
AGENCE = "Hersant Immobilier"
DEPTS_STOCK = {"45", "41"}
MAX_PAGES = 30
PHOTOS_PER_CARD = 10

_KEEP_TYPE = re.compile(
    r"maison|propriete|villa|ferme|fermette|longere|manoir|chateau|moulin|"
    r"demeure|domaine|gite|corps-de-ferme|pavillon",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


def _parse_card(card) -> dict | None:
    link = card.select_one("a.item__title")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type + pièces : segments d'URL "/vente/{id-ville}/{type}/t{N}/{id-slug}/"
    parts = [p for p in href.split("/") if p]
    type_seg = parts[2] if len(parts) > 2 else ""
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None  # type inconnu/ambigu → exclu par prudence
    type_bien = type_seg.replace("-", " ").strip() or "maison"

    pieces = None
    if len(parts) > 3:
        m = re.match(r"^t(\d+)$", parts[3])
        if m:
            pieces = int(m.group(1))

    # id_annonce : réf affichée, sinon id numérique du dernier segment
    ref_el = card.select_one(".item__reference")
    ref = ""
    if ref_el:
        m = re.search(r"R[ée]f\s*:?\s*(\S+)", ref_el.get_text(" ", strip=True))
        if m:
            ref = m.group(1)
    id_num = ""
    m = re.match(r"^(\d+)-", parts[-1]) if parts else None
    if m:
        id_num = m.group(1)
    id_annonce = ref or id_num or url

    loc_el = card.select_one(".title__content-1")
    ville, code_postal = parse_loc(loc_el.get_text(" ", strip=True) if loc_el else "")

    price_el = card.select_one(".item__price")
    prix = parse_price_digits(price_el.get_text(" ", strip=True) if price_el else "")

    text_el = card.select_one(".item__text-block")
    description = re.sub(
        r"\s+", " ", text_el.get_text(" ", strip=True) if text_el else ""
    ).strip()
    surface = parse_surface(description)

    titre = f"{type_bien.title()} {ville}".strip()

    photos = []
    for img in card.select("img.decorate__img, img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:") and "logo" not in src.lower():
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            if src not in photos:
                photos.append(src)
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
        "pieces": pieces,
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
            r = await get_with_retry(client, f"{BASE_URL}/vente/{page}")
            if r is None or r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select("article.property-listing-v2__container")
            if not cards:
                break

            # Fin de listing : stop si aucune carte nouvelle.
            page_ids = []
            for card in cards:
                a = card.select_one("a.item__title")
                page_ids.append(a.get("href", "") if a else "")
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

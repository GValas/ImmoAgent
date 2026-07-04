"""scrapers/anjou_immobilier.py — Anjou Immobilier (agence familiale de Segré-en-
Anjou Bleu, Haut-Anjou 49 — le stock déborde sur le sud Mayenne 53)

Méthode : scrape_simple (httpx) — SSR HTML (CMS « card_bien », même famille que
l'Agence Tourangelle / Hersant : listing catalogue unique paginé).
URL pattern : /vente/{page} (10 biens/page) → POST-FILTRE STRICT
code_postal[:2] (le CP est SUR la carte : .card_bien__localisation
"Segré-en-Anjou Bleu (49500)"), 0 fuite.

Cartes : div.card_bien
  - URL/type/id : a.card_bien__link[href] "/vente/{id-ville}/{type}/{id-slug}"
  - Titre    : texte direct du lien ("Maison 6 pièce(s)") + li
               .card_bien__title_part_3 ("4 chambre(s)", "153 m²")
  - CP/Ville : p.card_bien__localisation "Ville (49500)"
  - Prix     : p.card_bien__prix "236 050 €"
Le terrain/DPE/description ne sont pas sur la carte → laissés à gallery.py.

Types conservés : segment d'URL maison/propriété/longère… (appartement, terrain,
immeuble, local… exclus). Le scraper ne requête que si un département de son
stock (49/53) est demandé.

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

BASE_URL = "https://www.anjouimmobilier.fr"
SOURCE = "anjou_immobilier"
LABEL = "AnjouImmobilier"
AGENCE = "Anjou Immobilier"
DEPTS_STOCK = {"49", "53"}
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
    link = card.select_one("a.card_bien__link")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien : segment d'URL "/vente/{id-ville}/{type}/{id-slug}"
    parts = [p for p in href.split("/") if p]
    type_seg = parts[2] if len(parts) > 2 else ""
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None  # type inconnu/ambigu → exclu par prudence
    type_bien = type_seg.replace("-", " ").strip() or "maison"

    # id_annonce : id numérique du dernier segment "2257-maison-..."
    id_annonce = url
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_annonce = m.group(1)

    # Titre : texte direct du lien ("Maison 6 pièce(s)") + sous-items
    titre = re.sub(r"\s+", " ", link.get_text(" ", strip=True)).strip()
    pieces = None
    m = re.search(r"(\d+)\s*pi[eè]ce", titre, re.IGNORECASE)
    if m:
        pieces = int(m.group(1))
    chambres = None
    surface = None
    for li in card.select(".card_bien__title_part_3"):
        txt = li.get_text(" ", strip=True)
        m = re.search(r"(\d+)\s*chambre", txt, re.IGNORECASE)
        if m:
            chambres = int(m.group(1))
        m = re.search(r"([\d]+(?:[.,]\d+)?)\s*m²", txt)
        if m:
            surface = float(m.group(1).replace(",", "."))

    loc_el = card.select_one(".card_bien__localisation")
    ville, code_postal = parse_loc(loc_el.get_text(" ", strip=True) if loc_el else "")

    price_el = card.select_one(".card_bien__prix")
    prix = parse_price_digits(price_el.get_text(" ", strip=True) if price_el else "")

    photos = []
    for img in card.select("img"):
        src = img.get("data-src") or img.get("data-lazy") or img.get("src") or ""
        if src and not src.startswith("data:") and "logo" not in src.lower():
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": SOURCE,
        "url": url,
        "id_annonce": id_annonce,
        "titre": (titre or f"{type_bien.title()} {ville}").strip()[:150],
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
            cards = soup.select("div.card_bien")
            if not cards:
                break

            # Fin de listing : stop si aucune carte nouvelle (le CMS re-sert
            # la dernière page au-delà de la fin).
            page_ids = []
            for card in cards:
                a = card.select_one("a.card_bien__link")
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

"""scrapers/bellessort_immobilier.py — Bellessort Immobilier (agences Conlie &
Sillé-le-Guillaume, nord Sarthe 72 — stock surtout 72, débords 49/53)

Méthode : scrape_simple (httpx) — SSR HTML (CMS « atweb », cartes .atw-card,
même famille qu'anou_immobilier). Listing catalogue unique paginé :
URL pattern : /immobilier.php?recherche_offre=achat&page={N} (12 biens/page,
~5 pages ; au-delà le site re-sert les mêmes cartes) → POST-FILTRE STRICT
code_postal[:2] (le CP est SUR la carte : .ville-distance "VILLE - 72170" et
dans le slug de data-favorite-url "…/{ville}-{CP5}/…"), 0 fuite.

Cartes : div.atw-card
  - bouton .favorite-toggle : data-favorite-url (fiche), data-favorite-city,
    data-favorite-ref (id_annonce), data-favorite-title, data-favorite-image
  - .ville-distance      → "BEAUMONT SUR SARTHE - 72170" (ville + CP)
  - .atw-card-offer-type → "achat - maison" (type de bien)
  - .atw-card-price      → prix ; .atw-card-desc → extrait de description
  - .atw-card-details    → tuiles identifiées par l'icône SVG : superficie
    (surface), espace-jardin (terrain), piece-maison (pièces), chambre (chambres)

Types conservés : maison / propriété / longère… (appartement, terrain, local,
immeuble… exclus via l'offer-type et le segment de slug). Le scraper ne requête
que si un département de son stock (72/53) est demandé.

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
    standalone_main,
)

BASE_URL = "https://www.bellessort-immobilier.com"
LISTING_URL = f"{BASE_URL}/immobilier.php"
SOURCE = "bellessort_immobilier"
LABEL = "Bellessort"
AGENCE = "Bellessort Immobilier"
DEPTS_STOCK = {"72", "53", "49"}
MAX_PAGES = 15
PHOTOS_PER_CARD = 1     # une photo de couverture dispo sur la liste

_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|fermette|longere|longère|manoir|"
    r"chateau|château|moulin|demeure|domaine|gite|gîte|corps-de-ferme|pavillon",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)

_CP_SLUG = re.compile(r"-(\d{5})(?:/|$)")


def _tile_values(card) -> dict:
    """Tuiles .atw-card-details : champ identifié par le nom de fichier SVG."""
    out: dict[str, float | int | None] = {}
    for div in card.select(".atw-card-details > div"):
        img = div.select_one("img")
        src = (img.get("src") or "") if img else ""
        val_el = div.select_one("strong")
        txt = val_el.get_text(" ", strip=True) if val_el else ""
        num = re.sub(r"[^\d.,]", "", txt).replace(",", ".")
        if not num:
            continue
        try:
            val = float(num.replace(" ", ""))
        except ValueError:
            continue
        if "superficie" in src:
            out["surface"] = val
        elif "espace-jardin" in src:
            out["surface_terrain"] = val
        elif "piece" in src:
            out["pieces"] = int(val)
        elif "chambre" in src:
            out["chambres"] = int(val)
    return out


def _parse_card(card) -> dict | None:
    btn = card.select_one(".favorite-toggle")
    if not btn:
        return None
    url = btn.get("data-favorite-url") or ""
    if not url:
        return None
    if not url.startswith("http"):
        url = BASE_URL + url

    # Type de bien : libellé "achat - maison" + segment de slug en secours.
    offer_el = card.select_one(".atw-card-offer-type")
    offer = offer_el.get_text(" ", strip=True) if offer_el else ""
    type_txt = offer.split("-", 1)[-1].strip() if offer else ""
    type_src = type_txt or url
    if _EXCLUDE_TYPE.search(type_src) and not _KEEP_TYPE.search(type_src):
        return None
    if not _KEEP_TYPE.search(type_src):
        return None  # type inconnu/ambigu → exclu par prudence
    type_bien = type_txt.lower() or "maison"

    # Ville + CP : .ville-distance "BEAUMONT SUR SARTHE - 72170"
    ville, code_postal = "", ""
    vd = card.select_one(".ville-distance")
    if vd:
        m = re.match(r"(.*?)\s*-\s*(\d{5})\s*$", vd.get_text(" ", strip=True))
        if m:
            ville, code_postal = m.group(1).strip().title(), m.group(2)
    if not code_postal:                       # secours : CP du slug de la fiche
        m = _CP_SLUG.search(url)
        if m:
            code_postal = m.group(1)
    if not ville:
        ville = (btn.get("data-favorite-city") or "").title()

    ref = btn.get("data-favorite-ref") or ""
    id_annonce = ref or card.get("id") or url

    titre = (btn.get("data-favorite-title") or "").strip()
    desc_el = card.select_one(".atw-card-desc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    price_el = card.select_one(".atw-card-price")
    prix = parse_price_digits(price_el.get_text(" ", strip=True) if price_el else "")

    tiles = _tile_values(card)

    photo = btn.get("data-favorite-image") or ""
    photos = [photo] if photo.startswith("http") else []

    return {
        "source": SOURCE,
        "url": url,
        "id_annonce": id_annonce,
        "titre": (titre or f"{type_bien.title()} {ville}").strip()[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": tiles.get("surface"),
        "surface_terrain": tiles.get("surface_terrain"),
        "pieces": tiles.get("pieces"),
        "chambres": tiles.get("chambres"),
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
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
            r = await get_with_retry(
                client, f"{LISTING_URL}?recherche_offre=achat&page={page}"
            )
            if r is None or r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select("div.atw-card")
            if not cards:
                break

            # Fin de listing : le CMS re-sert les mêmes cartes au-delà de la
            # dernière page → stop si aucune carte nouvelle.
            page_ids = [cd.get("id", "") for cd in cards]
            new_ids = [i for i in page_ids if i and i not in seen_card_ids]
            if page > 1 and not new_ids:
                break
            seen_card_ids.update(page_ids)

            for card in cards:
                if card.get("id", "") not in new_ids and page > 1:
                    continue
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

"""scrapers/turone_immobilier.py — Turone Immobilier (Chinon, 37 — pays de
Chinon / Bourgueil / Richelieu, débord Vienne 86 hors zone)

Méthode : scrape_simple (httpx) — SSR HTML, catalogue type osCommerce
(« /catalog/ », cartes div.product-listing). Agence indépendante depuis 2005,
~50 biens à la vente, maisons anciennes / propriétés de caractère.

URL pattern : /annonces/transaction/vente.html?manufacturers_id=transaction&page={N}
(10 cartes/page ; le serveur REBOUCLE sur la dernière page pour page>fin →
arrêt sur « page entière déjà vue », dédup id). PAS de filtre département
côté serveur → post-filtre STRICT code_postal[:2] (CP SUR la carte :
.products-localisation « 37140 BOURGUEIL »), écarte le débord 86.

Cartes : div.product-listing
  - URL/fiche : a[href*="fiches/"] ; id numérique dans l'URL (…_60838967/)
  - Titre     : .products-name ; Prix : .products-price
  - CP/Ville  : .products-localisation « 37140 BOURGUEIL »
  - Descr.    : .products-description (complète → surface/pièces regex)
  - Photo     : img.photo-listing (chemin relatif ../office5/…)

Ne requête que si le 37 est demandé.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import (
    _jitter,
    get_with_retry,
    make_client,
    parse_int,
    parse_price_digits,
    parse_surface,
    standalone_main,
)

BASE_URL = "https://www.turoneimmobilier.com"
LIST_URL = (
    BASE_URL + "/annonces/transaction/vente.html?manufacturers_id=transaction&page={page}"
)
SOURCE = "turone_immobilier"
LABEL = "TuroneImmo"
AGENCE = "Turone Immobilier"
DEPTS_AGENCE = {"37"}   # stock 37 + débord 86 (écarté au post-filtre)
MAX_PAGES = 12
PHOTOS_PER_CARD = 5

_EXCLUDE_TYPE = re.compile(
    r"appartement|duplex|studio|terrain|local commercial|garage|parking|bureau|"
    r"fonds de commerce|immeuble de rapport",
    re.IGNORECASE,
)
_FICHE_ID_RE = re.compile(r"_(\d+)/")


def _parse_card(card) -> dict | None:
    link = card.find("a", href=re.compile(r"fiches/"))
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = BASE_URL + "/" + href.lstrip("./").lstrip("/")

    m = _FICHE_ID_RE.search(href)
    id_annonce = m.group(1) if m else url

    name_el = card.select_one(".products-name")
    titre = re.sub(r"\s*-\s*$", "", name_el.get_text(" ", strip=True) if name_el else "")

    desc_el = card.select_one(".products-description")
    description = re.sub(
        r"\s+", " ", desc_el.get_text(" ", strip=True) if desc_el else ""
    ).strip()

    if _EXCLUDE_TYPE.search(titre):
        return None

    loc_el = card.select_one(".products-localisation")
    loc_text = loc_el.get_text(" ", strip=True) if loc_el else ""
    m_loc = re.match(r"(\d{5})\s+(.+)", loc_text)
    if not m_loc:
        return None
    code_postal, ville = m_loc.group(1), m_loc.group(2).title()

    price_el = card.select_one(".products-price")
    prix = None
    if price_el:
        # « 523 900 € dont 4.78% TTC d'honoraires » → premier montant seulement
        m_p = re.search(r"([\d\s\xa0.,]+)\s*€", price_el.get_text(" ", strip=True))
        if m_p:
            prix = parse_price_digits(m_p.group(1))

    # Surface / pièces / chambres / terrain depuis titre + description.
    # NB : ne PAS prendre le premier « NNN m² » venu (souvent le terrain) —
    # seulement les tournures explicitement « habitables ».
    texte = f"{titre} {description}"
    surface = parse_surface(texte)
    if surface is None:
        for m_s in re.finditer(r"(\d{2,3}(?:[.,]\d+)?)\s*m[²2]\b", texte):
            avant = texte[max(0, m_s.start() - 25):m_s.start()].lower()
            apres = texte[m_s.end():m_s.end() + 20].lower()
            if re.search(r"terrain|parcelle|jardin|cour|grange|d[ée]pendance", avant):
                continue
            if re.search(r"de terrain|de parcelle|de jardin", apres):
                continue
            try:
                f = float(m_s.group(1).replace(",", "."))
            except ValueError:
                continue
            if 20 <= f <= 600:
                surface = f
                break
    surface_terrain = None
    m_t2 = re.search(
        r"terrain\D{0,40}?(\d[\d\s\xa0]{1,8})\s*(m[²2]|ha|hectare)", texte, re.IGNORECASE)
    if m_t2:
        try:
            v = float(re.sub(r"[\s\xa0]", "", m_t2.group(1)))
            surface_terrain = v * 10000 if m_t2.group(2).lower().startswith("h") else v
        except ValueError:
            pass
    pieces = parse_int(r"(\d+)\s*pi[eè]ces?", texte)
    chambres = parse_int(r"(\d+)\s*chambres?", texte)

    type_bien = "maison"
    m_t = re.search(
        r"(propri[ée]t[ée]|longère|manoir|château|moulin|demeure|ferme|pavillon|"
        r"ensemble immobilier|maison)", titre, re.IGNORECASE)
    if m_t:
        type_bien = m_t.group(1).lower()

    photos = []
    for img in card.select("img.photo-listing"):
        src = img.get("src") or ""
        if src and not src.startswith("data:"):
            src = BASE_URL + "/" + src.lstrip("./").lstrip("/")
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
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": AGENCE,
    }


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    cibles = departements & DEPTS_AGENCE
    if not cibles:
        return []

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    biens: list[dict] = []
    seen_ids: set[str] = set()

    async with make_client() as client:
        for page in range(1, MAX_PAGES + 1):
            r = await get_with_retry(client, LIST_URL.format(page=page))
            # NB : le serveur renvoie par intermittence un statut 500 avec un
            # corps pourtant complet → on juge sur les cartes, pas sur le code.
            if r is None or not r.text:
                break
            cards = BeautifulSoup(r.text, "html.parser").select("div.product-listing")
            if not cards:
                break

            # Arrêt sur les cartes BRUTES (dédup id) : le serveur reboucle sur
            # la dernière page quand page > fin.
            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue
                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                new_on_page += 1
                # Garde-fou département STRICT : écarte notamment le 86
                cp = bien["code_postal"]
                if cp[:2] not in cibles:
                    continue
                bien["departement"] = cp[:2]
                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue
                biens.append(bien)

            if new_on_page == 0:   # page entière déjà vue → fin du catalogue
                break
            await asyncio.sleep(_jitter(0.5))

    print(f"[{LABEL}] {len(biens)} annonces")
    return biens


if __name__ == "__main__":
    standalone_main(search, AGENCE)

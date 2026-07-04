"""scrapers/agence_berry_nivernais.py — Agence Berry-Nivernais (La Charité-sur-Loire, 58)

Méthode : scrape_simple (httpx) — SSR HTML (CMS La Boîte Immo, images staticlbi.com,
gabarit ancien « panelBien », distinct du property-listing-v2 de fnaim_beugnot).
Agence indépendante installée depuis 1970 place du Général de Gaulle à
La Charité-sur-Loire — stock Nièvre (58) + frange Cher (18), maisons de caractère.

URL pattern : /a-vendre/{page} (uniquement des ventes, ~10 cartes li.panelBien/page,
~86 biens observés). PAS de filtre département côté serveur (agence mono-secteur) →
post-filtre STRICT code_postal[:2] ∈ départements demandés (CP présent sur chaque
carte : « Maison 236 m² - 8 Pièces - La Charité-Sur-Loire (58400) »).

Ne requête que si 58 ou 18 est demandé.

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
    parse_loc,
    parse_price_digits,
    standalone_main,
)

BASE_URL = "https://www.immoberrynivernais.fr"
DEPTS_AGENCE = {"58", "18"}   # Nièvre + frange Cher
MAX_PAGES = 15

_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


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
            r = await get_with_retry(client, f"{BASE_URL}/a-vendre/{page}")
            if r is None or r.status_code != 200:
                break
            cards = BeautifulSoup(r.text, "html.parser").select("li.panelBien")
            if not cards:
                break

            # L'arrêt de pagination se fonde sur les cartes BRUTES (dédup id),
            # pas sur les biens gardés : avec des bornes prix serrées, des pages
            # entières sont filtrées alors que la suite du stock reste à lire.
            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue
                aid = bien.get("id_annonce") or bien.get("url")
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                new_on_page += 1
                # Garde-fou département STRICT : CP obligatoire et dans les cibles
                cp = bien.get("code_postal") or ""
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

            if new_on_page == 0:   # page entière déjà vue → fin de la liste
                break
            await asyncio.sleep(_jitter(0.5))

    print(f"[BerryNivernais] {len(biens)} annonces")
    return biens


def _parse_card(card) -> dict | None:
    link = card.select_one("a.btn-listing")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # h2 : « Maison 236 m² - 8 Pièces - La Charité-Sur-Loire (58400) »
    h2 = card.select_one(".bienTitle h2")
    h2_text = h2.get_text(" ", strip=True) if h2 else ""
    type_bien = (h2_text.split() or [""])[0].lower()
    if _EXCLUDE_TYPE.search(type_bien):
        return None

    # Séparateur « - » ESPACÉ (les tirets des communes n'ont pas d'espaces)
    loc_part = re.split(r"\s-\s", h2_text)[-1]
    ville, code_postal = parse_loc(loc_part)
    if not code_postal:
        return None

    prix_el = card.select_one("[itemprop=price]")
    prix = parse_price_digits(
        prix_el.get("content") or prix_el.get_text(strip=True)) if prix_el else None

    surface = None
    m = re.search(r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m²", h2_text)
    if m:
        try:
            surface = float(re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", "."))
        except ValueError:
            pass
    pieces = parse_int(r"(\d+)\s*Pi[eè]ces?", h2_text)

    h1 = card.select_one(".bienTitle h1")
    titre = h1.get_text(" ", strip=True) if h1 else ""
    # Le h1 liste est tronqué (« Maison de... ») → slug de l'URL en secours
    if not titre or titre.endswith("..."):
        slug = re.sub(r"^/?\d+-", "", href.strip("/")).replace(".html", "")
        titre = slug.replace("-", " ").capitalize() or h2_text

    desc_el = card.select_one("[itemprop=description]")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    ref_el = card.select_one(".ref")
    ref = re.sub(r"(?i)^ref\s*", "", ref_el.get_text(strip=True)) if ref_el else ""
    m_id = re.match(r"^/?(\d+)-", href.lstrip("/"))
    id_annonce = ref.replace(" ", "") or (m_id.group(1) if m_id else url)

    photos = []
    img = card.select_one("img[itemprop=image]")
    if img:
        src = img.get("src") or ""
        if src.startswith("//"):
            src = "https:" + src
        if src and not src.startswith("data:"):
            photos.append(src)

    return {
        "source": "agence_berry_nivernais",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien or "maison",
        "description": description[:1200],
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Agence Berry-Nivernais",
    }


if __name__ == "__main__":
    standalone_main(search, "Agence Berry-Nivernais")

"""scrapers/tradim_immobilier.py — Tradim Immobilier (Gien, Loiret 45)

Méthode : scrape_simple (httpx) — SSR HTML (CMS eZ Platform).
Petite agence indépendante de Gien (45), secteur Gien / Montargis / Sully.

URL liste : /Acheter   (tous les biens à vendre sur une page)
Cartes : div.property-details-full
  - ville  : .ezstring-field   « MONTARGIS »   (la carte n'a PAS de code postal)
  - titre  : p.orange          « vente Propriété Montargis 530m² 4,6 Ha Étang… »
  - prix   : .price            « 898 000 € »
  - URL    : a[href^='/Acheter/...']
  La surface est parfois dans le titre (« 530m² ») ; pas de pièces fiables en liste.

Filtre département : aucun CP sur la carte → on résout le NOM DE COMMUNE en
(dept, CP) via geo.api.gouv.fr (scrapers/_geo_resolve.py), puis POST-FILTRE STRICT
code_postal[:2] ∈ départements cibles → écarte d'office les biens hors-France
(ex. villa en Thaïlande) et hors-zone. 0 fuite.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_price, standalone_main
from scrapers._geo_resolve import resolve_dept

BASE_URL = "https://www.tradim-immobilier.com"
LIST_URL = BASE_URL + "/Acheter"
PHOTOS_PER_CARD = 1

_TYPE_RE = re.compile(r"maison|propri[eé]t[eé]|longère|longere|ferme|manoir|moulin|demeure|château|chateau|villa", re.I)


def _parse_card(card) -> dict | None:
    a = card.select_one("a[href]")
    href = a.get("href") if a else ""
    if not href or "/Acheter/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    city_el = card.select_one(".ezstring-field")
    ville = city_el.get_text(" ", strip=True) if city_el else ""

    title_el = card.select_one("p.orange")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    price_el = card.select_one(".price")
    prix = parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    surface = None
    m = re.search(r"(\d[\d\s\xa0]*)\s*m²", titre)
    if m:
        try:
            v = float(re.sub(r"[\s\xa0]", "", m.group(1)))
            if 8 <= v <= 2000:
                surface = v
        except ValueError:
            surface = None

    type_bien = "maison"
    mt = _TYPE_RE.search(titre)
    if mt:
        type_bien = mt.group(0).lower()

    m_id = re.search(r"/Acheter/(.+?)/?$", href)
    id_annonce = (m_id.group(1)[:60] if m_id else url)

    photos = []
    img = card.select_one("img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            photos.append(src if src.startswith("http") else BASE_URL + src)

    return {
        "source": "tradim_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150] or f"{type_bien.title()} {ville.title()}",
        "type_bien": type_bien,
        "description": "",
        "departement": "",
        "ville": ville.title()[:80],
        "code_postal": "",
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Tradim Immobilier",
    }


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen: set[str] = set()
    geo_cache: dict[str, tuple[str, str]] = {}

    async with make_client() as client:
        r = await get_with_retry(client, LIST_URL)
        if r is None or r.status_code != 200:
            print(f"[Tradim] Liste inaccessible (status {getattr(r, 'status_code', '?')})")
            return results
        cards = BeautifulSoup(r.text, "html.parser").select("div.property-details-full")

        kept: dict[str, int] = {}
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien or not bien.get("ville"):
                continue
            dept, cp = await resolve_dept(client, bien["ville"], geo_cache)
            if not cp or cp[:2] not in departements:
                continue
            bien["code_postal"] = cp
            bien["departement"] = dept or cp[:2]
            aid = bien.get("id_annonce")
            if aid in seen:
                continue
            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue
            seen.add(aid)
            results.append(bien)
            kept[cp[:2]] = kept.get(cp[:2], 0) + 1
            await asyncio.sleep(0.1)

    print(f"[Tradim] {len(cards)} cartes → {len(results)} retenues par dept {kept}")
    return results


if __name__ == "__main__":
    standalone_main(search, "Tradim Immobilier")

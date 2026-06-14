"""scrapers/bannier_immobilier.py — Bannier Immobilier (Orléans & Sologne, 45)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress, thème RealHomes/Homez).
Agence indépendante d'Orléans, secteur Loiret (45) et Sologne.

URL liste ventes : /property-status/vente/?paged=N   (pagination ?paged=N)
Cartes : article.property-item
  - prix     : .price-text                « 201 400 »
  - titre    : .property-title            « Maison/villa en vente à Orléans »
               → la ville suit « à … » (la carte n'expose PAS de code postal)
  - pièces/chambres/surface : texte de la carte (« 4 Pièces 3 Chambres 96 m² »)
  - coords   : data-latitude / data-longitude (position précise)
  - URL+slug : a[href*='/property/']  (slug « maison-villa-vente-{ville}-... »)

Filtre département : la carte n'a PAS de code postal ; on extrait la VILLE
(du titre « … à VILLE », repli sur le slug) et on la résout en (dept, CP) via
geo.api.gouv.fr (scrapers/_geo_resolve.py), puis POST-FILTRE STRICT
code_postal[:2] ∈ départements cibles → 0 fuite.

On ne garde que les annonces maison/villa (le slug commence par « maison-villa »).

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_int, parse_price, standalone_main
from scrapers._geo_resolve import resolve_dept

BASE_URL = "https://www.bannier-immobilier.fr"
LIST_URL = BASE_URL + "/property-status/vente/?paged={page}"
MAX_PAGES = 8
PHOTOS_PER_CARD = 3


def _city_from(title: str, href: str) -> str:
    m = re.search(r"\b[àa]\s+(.+)$", title or "", re.I)
    if m:
        return m.group(1).strip()
    # repli : slug « maison-villa-vente-{ville}-{seq}_{id} »
    m = re.search(r"/property/[a-z-]*?vente-(.+?)-\d+_\d+/?$", href or "")
    if m:
        return m.group(1).replace("-", " ").strip()
    return ""


def _parse_card(card) -> dict | None:
    a = card.select_one("a[href*='/property/']")
    href = a.get("href") if a else ""
    if not href:
        return None
    # on ne traite que les maisons/villas
    if "maison-villa" not in href:
        return None

    title_el = card.select_one(".property-title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    ville = _city_from(titre, href)

    price_el = card.select_one(".price-text")
    prix = parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    txt = card.get_text(" ", strip=True)
    pieces = parse_int(r"(\d+)\s*Pi[èe]ces?", txt)
    chambres = parse_int(r"(\d+)\s*Chambres?", txt)
    surface = None
    m = re.search(r"([\d\s\xa0]+)\s*m²", txt)
    if m:
        try:
            surface = float(re.sub(r"[\s\xa0]", "", m.group(1)))
        except ValueError:
            surface = None

    lat = card.get("data-latitude")
    lon = card.get("data-longitude")

    photos = []
    di = card.get("data-images")
    if di:
        try:
            import json
            photos = [u for u in json.loads(di) if isinstance(u, str)]
        except Exception:
            photos = []
    if not photos:
        img = card.select_one("img[data-src]")
        if img and img.get("data-src"):
            photos = [img.get("data-src")]

    m_id = re.search(r"_(\d+)/?$", href)
    id_annonce = m_id.group(1) if m_id else href

    bien = {
        "source": "bannier_immobilier",
        "url": href,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": "",
        "departement": "",
        "ville": ville[:80],
        "code_postal": "",
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Bannier Immobilier",
    }
    try:
        if lat and lon:
            bien["latitude"] = float(lat)
            bien["longitude"] = float(lon)
    except ValueError:
        pass
    return bien


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()
    kept_ids: set[str] = set()
    geo_cache: dict[str, tuple[str, str]] = {}

    async with make_client() as client:
        for page in range(1, MAX_PAGES + 1):
            r = await get_with_retry(client, LIST_URL.format(page=page))
            if r is None or r.status_code != 200:
                break
            cards = BeautifulSoup(r.text, "html.parser").select("article.property-item")
            if not cards:
                break

            new_cards = 0
            kept = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien or not bien.get("ville"):
                    continue
                aid = bien.get("id_annonce")
                if aid not in seen_ids:
                    seen_ids.add(aid)
                    new_cards += 1
                dept, cp = await resolve_dept(client, bien["ville"], geo_cache)
                if not cp or cp[:2] not in departements:
                    continue
                bien["code_postal"] = cp
                bien["departement"] = dept or cp[:2]
                if aid in kept_ids:
                    continue
                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue
                kept_ids.add(aid)
                results.append(bien)
                kept += 1

            print(f"[Bannier] Page {page}: {len(cards)} cartes ({new_cards} nouvelles), {kept} retenues (cumul {len(results)})")
            if new_cards == 0 and page > 1:
                break
            await asyncio.sleep(0.5)

    print(f"[Bannier] Total {len(results)} annonces (départements cibles)")
    return results


if __name__ == "__main__":
    standalone_main(search, "Bannier Immobilier")

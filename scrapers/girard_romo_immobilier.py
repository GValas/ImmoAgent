"""scrapers/girard_romo_immobilier.py — Girard Immobilier (Romo Immobilier, Romorantin)

Méthode : scrape_simple (httpx) — SSR HTML.
Agence de Romorantin-Lanthenay (41) avec antennes Selles-sur-Cher et Blois ;
couvre le sud Loir-et-Cher (41) et déborde sur l'Indre (36, cible) — communes
type La Vernelle (36).

NB : DISTINCT de scrapers/cabinet_girard.py (Cabinet Girard à Nevers, 58) — autre
agence, autre domaine (girard-immobilier.com vs cabinet-girard-immobilier.fr).

URL liste maisons (une seule page) : /maisons-a-vendre.html
Cartes : div.property-listing
  - titre/pièces/chambres : h4.listing-name   « Maison 4 pièces - 4 chambres »
  - localisation          : .listing-location  « ROMORANTIN-LANTHENAY » (PAS de CP)
  - features              : .listing-features-info  « Ch. : 4 | Sdb : 1 | 137 m² hab. »
  - prix                  : .list-pr            « 189 000 € »
  - URL                   : a[href*='maison-a-vendre-']  (slug ville + id)

Filtre département : aucun code postal sur la carte → on résout le NOM DE COMMUNE
en (dept, CP) via geo.api.gouv.fr (scrapers/_geo_resolve.py), puis POST-FILTRE
STRICT code_postal[:2] ∈ départements cibles → 0 fuite (ex. communes 18/37 hors
zone écartées, La Vernelle 36 conservée).

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_int, parse_price, standalone_main
from scrapers._geo_resolve import resolve_dept

BASE_URL = "https://girard-immobilier.com"
LIST_URL = BASE_URL + "/maisons-a-vendre.html"
PHOTOS_PER_CARD = 1


def _parse_card(card) -> dict | None:
    a = card.select_one("a[href*='maison-a-vendre-']")
    href = a.get("href") if a else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + "/" + href.lstrip("/")

    name_el = card.select_one(".listing-name")
    titre = name_el.get_text(" ", strip=True) if name_el else ""
    pieces = parse_int(r"(\d+)\s*pi[èe]ces?", titre)

    loc_el = card.select_one(".listing-location")
    ville = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville = re.sub(r"^\s*", "", ville).strip()

    feat = card.select_one(".listing-features-info")
    ftxt = feat.get_text(" | ", strip=True) if feat else ""
    chambres = parse_int(r"Ch\.?\s*:\s*(\d+)", ftxt)
    # La surface est dans son propre <li> (« 137 m² hab. ») : on parcourt les <li>
    # pour éviter de capturer le chiffre voisin (« Sdb : 1 » + « 137 m² »).
    surface = None
    for li in (feat.select("li") if feat else []):
        lt = li.get_text(" ", strip=True)
        m = re.search(r"^\s*([\d\s\xa0]+)\s*m²", lt)
        if m:
            try:
                surface = float(re.sub(r"[\s\xa0]", "", m.group(1)))
            except ValueError:
                surface = None
            break

    price_el = card.select_one(".list-pr")
    prix = parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    m_id = re.search(r"-(\d+)\.html", href)
    id_annonce = m_id.group(1) if m_id else url

    photos = []
    img = card.select_one("img")
    if img and img.get("src") and not img.get("src").startswith("data:"):
        src = img.get("src")
        photos.append(src if src.startswith("http") else BASE_URL + "/" + src.lstrip("/"))

    return {
        "source": "girard_romo_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150] or f"Maison {ville.title()}",
        "type_bien": "maison",
        "description": "",
        "departement": "",
        "ville": ville.title()[:80],
        "code_postal": "",
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Girard Immobilier (Romorantin)",
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
            print(f"[Girard-Romo] Liste inaccessible (status {getattr(r, 'status_code', '?')})")
            return results
        cards = BeautifulSoup(r.text, "html.parser").select("div.property-listing")

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

    print(f"[Girard-Romo] {len(cards)} cartes → {len(results)} retenues par dept {kept}")
    return results


if __name__ == "__main__":
    standalone_main(search, "Girard Immobilier (Romorantin)")

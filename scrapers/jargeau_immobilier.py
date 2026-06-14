"""scrapers/jargeau_immobilier.py — Jargeau Immobilier (Jargeau, Loiret 45)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress).
Agence du Val d'Or / Jargeau (45), secteur est-orléanais : Jargeau,
Châteauneuf-sur-Loire, Saint-Denis-de-l'Hôtel, Sully…

URL liste ventes : /biens/?_statut_du_bien=vente   (toutes les ventes, une page)
Cartes : article.portefeuille__item
  - prix  : .price          1er nœud texte « 365 750 € » (+ span « soit X €/m² »)
  - ville : .localisation   « Bricy » / « Châteauneuf-sur-Loire » (PAS de code postal)
  - URL   : a.portefeuille__item__pic[href*='/bien/']
  - surface : déduite de prix ÷ (prix/m²) du span « soit X €/m² » (fiable)

Filtre département : aucun code postal sur la carte → on résout le NOM DE COMMUNE
(.localisation) en (dept, CP) via geo.api.gouv.fr (scrapers/_geo_resolve.py), puis
POST-FILTRE STRICT code_postal[:2] ∈ départements cibles → 0 fuite.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_price, standalone_main
from scrapers._geo_resolve import resolve_dept

BASE_URL = "https://www.jargeau-immobilier.com"
LIST_URL = BASE_URL + "/biens/?_statut_du_bien=vente"
PHOTOS_PER_CARD = 3

_EXCLUDE = re.compile(r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds", re.I)


def _parse_card(card) -> dict | None:
    a = card.select_one("a[href*='/bien/']")
    href = a.get("href") if a else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    loc_el = card.select_one(".localisation")
    ville = loc_el.get_text(" ", strip=True) if loc_el else ""

    price_el = card.select_one(".price")
    prix = None
    prix_m2 = None
    if price_el:
        # 1er nœud texte = prix ; le span = « soit X €/m² »
        first = price_el.find(string=True)
        prix = parse_price(first if first else price_el.get_text(" ", strip=True))
        span = price_el.select_one("span")
        if span:
            m = re.search(r"([\d\s\xa0,\.]+)\s*€/m", span.get_text(" ", strip=True))
            if m:
                v = m.group(1).replace(" ", "").replace("\xa0", "").replace(",", ".")
                v = re.sub(r"\.(?=\d{3}\b)", "", v)  # point milliers éventuel
                try:
                    prix_m2 = float(v)
                except ValueError:
                    prix_m2 = None
    surface = None
    if prix and prix_m2 and prix_m2 > 0:
        surface = round(prix / prix_m2, 0)

    content = card.select_one(".portefeuille__item__content")
    ctxt = content.get_text(" ", strip=True) if content else ""
    # type depuis le titre (1ère ligne après la ville)
    type_bien = "maison"
    if re.search(r"longère|longere|fermette|corps de ferme|propri[eé]t[eé]|manoir|demeure", ctxt, re.I):
        m = re.search(r"(longère|longere|fermette|corps de ferme|propri[eé]t[eé]|manoir|demeure)", ctxt, re.I)
        type_bien = m.group(1).lower()
    elif _EXCLUDE.search(ctxt.split(".")[0][:60]):
        return None  # appartement/terrain/immeuble explicite

    titre = ""
    if content:
        h = content.find(["h2", "h3", "h4"]) or content.find("p")
        titre = (h.get_text(" ", strip=True) if h else "")[:150]
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    m_id = re.search(r"/bien/([^/]+)/?$", url)
    id_annonce = m_id.group(1) if m_id else url

    photos = []
    img = card.select_one("img")
    if img and img.get("src") and not img.get("src").startswith("data:"):
        photos.append(img.get("src"))

    return {
        "source": "jargeau_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre,
        "type_bien": type_bien,
        "description": ctxt[:800],
        "departement": "",
        "ville": ville[:80],
        "code_postal": "",
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Jargeau Immobilier",
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
            print(f"[Jargeau] Liste inaccessible (status {getattr(r, 'status_code', '?')})")
            return results
        cards = BeautifulSoup(r.text, "html.parser").select("article.portefeuille__item")

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

    print(f"[Jargeau] {len(cards)} cartes → {len(results)} retenues par dept {kept}")
    return results


if __name__ == "__main__":
    standalone_main(search, "Jargeau Immobilier")

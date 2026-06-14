"""scrapers/blayez_immobilier.py — Blayez Immobilier (Brive/Tulle/Egletons/Ussel, Corrèze 19)

Méthode : scrape_simple (httpx) — SSR HTML (template Wizi-v2 « property-listing-v2__item »).
URL pattern : /vente/{page}   (liste NATIONALE de l'agence, ~10 biens/page ; pas de
              slug département côté serveur). On pagine tout puis POST-FILTRE STRICT
              sur code_postal[:2] ∈ départements cibles → 0 fuite.
Cartes : article.property-listing-v2__item
  .title__content-1                       → ville
  .title__content-2                       → « (CP) »
  .property-listing-v2__item-compo        → « N pièces - NNN m² »
  h2 > a.property-listing-v2__item-text    → titre + URL détail
  .property-listing-v2__price-value       → prix
  .property-listing-v2__item-reference    → « Ref : XXXX »

Particularité : réseau Blayez Immobilier (5 agences Corrèze 19) — hors zone cible
actuelle. Scraper du segment Sud-Ouest/Limousin, post-filtre CP → 0 fuite.
dernier_test : 0 stock dans la zone (la totalité du stock est en Corrèze).

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_price

BASE_URL = "https://www.blayez-immobilier.fr"
SOURCE = "blayez_immobilier"
LABEL = "BlayezImmo"
MAX_PAGES = 30

# Type de bien dans le 3e segment d'URL (/vente/{id-ville}/{type}/...).
_KEEP_TYPE = re.compile(r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|maison-de-village", re.IGNORECASE)


def _type_seg(href: str) -> str:
    parts = [p for p in href.split("/") if p]
    # parts: ['vente', '{id}-{ville}', '{type}', ...]
    return parts[2] if len(parts) > 2 else ""


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen: set[str] = set()

    async with make_client() as client:
        for page in range(1, MAX_PAGES + 1):
            r = await get_with_retry(client, f"{BASE_URL}/vente/{page}")
            if r is None or r.status_code != 200:
                break
            cards = BeautifulSoup(r.text, "html.parser").select(
                "article.property-listing-v2__item"
            )
            if not cards:
                break
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue
                cp = bien.get("code_postal") or ""
                if not cp or cp[:2] not in departements:
                    continue   # POST-FILTRE STRICT (0 fuite)
                bien["departement"] = cp[:2]
                aid = bien["id_annonce"]
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
            await asyncio.sleep(0.5)

    print(f"[{LABEL}] {len(results)} annonces (post-filtre dept)")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a.property-listing-v2__item-text") or card.find("a", href=True)
    if not link:
        return None
    href = link.get("href", "")
    if not _KEEP_TYPE.search(_type_seg(href)):
        return None   # terrain-a-batir, studio, appartement, immeuble… → on écarte
    url = href if href.startswith("http") else BASE_URL + href

    ville_el = card.select_one(".title__content-1")
    ville = ville_el.get_text(" ", strip=True) if ville_el else ""
    cp_el = card.select_one(".title__content-2")
    cp = ""
    if cp_el:
        m = re.search(r"(\d{5})", cp_el.get_text())
        if m:
            cp = m.group(1)

    titre = link.get_text(" ", strip=True) or f"Maison {ville}".strip()

    price_el = card.select_one(".property-listing-v2__price-value")
    prix = parse_price(price_el.get_text(" ", strip=True)) if price_el else None

    ref_el = card.select_one(".property-listing-v2__item-reference")
    ref = ""
    if ref_el:
        m = re.search(r"Ref\.?\s*:?\s*([0-9A-Za-z]+)", ref_el.get_text(" ", strip=True), re.IGNORECASE)
        if m:
            ref = m.group(1)
    id_annonce = ref or url

    pieces = surface = None
    compo = card.select_one(".property-listing-v2__item-compo")
    if compo:
        t = compo.get_text(" ", strip=True)
        m_p = re.search(r"(\d+)\s*pi[eè]ce", t, re.IGNORECASE)
        if m_p:
            pieces = int(m_p.group(1))
        m_s = re.search(r"([\d\s\xa0]+)\s*m²", t)
        if m_s:
            try:
                surface = float(re.sub(r"[\s\xa0]", "", m_s.group(1)))
            except ValueError:
                pass
    if pieces is None:
        m_t = re.search(r"/t(\d+)/", href)
        if m_t:
            pieces = int(m_t.group(1))

    photos = []
    img = card.find("img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)

    return {
        "source": SOURCE,
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": "",
        "departement": cp[:2] if cp else "",
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Blayez Immobilier",
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Blayez Immobilier")

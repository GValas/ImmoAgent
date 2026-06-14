"""scrapers/parent_immobilier.py — Parent Immobilier (La Souterraine, Creuse 23)

Méthode : scrape_simple (httpx) — SSR HTML (template Wizi/Apimo « property-listing-v2 »).
URL pattern : /vente/{page}   (liste unique paginée, ~10 biens/page, ~47 au total ;
              le site couvre la Creuse 23 + franges Indre 36 / Haute-Vienne 87 / Cher 18).
              → PAS de filtre département côté serveur ; on pagine tout puis on
              POST-FILTRE STRICT sur code_postal[:2] ∈ départements cibles → 0 fuite.
Cartes : article.property-listing-v2__container
  .item__title > h2 > span.title__content-1  → « Ville (CP) »
  .item__title > h2 > span.title__content-2  → titre descriptif
  .item__price .__price-value                → prix
  .item__reference                           → « Réf : NN »
  href /vente/{id}-{ville}/maison/t{N}/...    → pièces depuis le segment tN

Particularité : surface non exposée en liste (enrichie en page détail par gallery.py).

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_price, parse_surface

BASE_URL = "https://www.parent-immobilier.fr"
SOURCE = "parent_immobilier"
LABEL = "ParentImmo"
AGENCE = "Parent Immobilier"
MAX_PAGES = 12

_EXCLUDE = re.compile(r"appartement|terrain|immeuble|local|commerce|garage|parking|fonds", re.IGNORECASE)


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
                "article.property-listing-v2__container"
            )
            if not cards:
                break
            new = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue
                cp = bien.get("code_postal") or ""
                # POST-FILTRE STRICT département (0 fuite)
                if not cp or cp[:2] not in departements:
                    continue
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
                new += 1
            if new == 0 and page > 1:
                # page sans bien retenu mais peut-être tout hors-zone : on continue
                # jusqu'à épuisement réel des cartes (déjà géré par `if not cards`).
                pass
            await asyncio.sleep(0.5)

    print(f"[{LABEL}] {len(results)} annonces (post-filtre dept)")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a.item__title") or card.find("a", href=re.compile(r"^/vente/\d+-"))
    if not link:
        return None
    href = link.get("href", "")
    if _EXCLUDE.search(href):
        return None
    url = href if href.startswith("http") else BASE_URL + href

    loc_el = card.select_one(".title__content-1")
    ville, cp = _parse_loc(loc_el.get_text(" ", strip=True) if loc_el else "")

    titre_el = card.select_one(".title__content-2")
    titre = titre_el.get_text(" ", strip=True) if titre_el else ""
    if not titre:
        titre = f"Maison {ville}".strip()

    price_el = card.select_one(".item__price .__price-value") or card.select_one(".item__price")
    prix = parse_price(price_el.get_text(" ", strip=True)) if price_el else None

    ref_el = card.select_one(".item__reference")
    ref = ""
    if ref_el:
        m = re.search(r"R[ée]f\.?\s*:?\s*([0-9A-Za-z]+)", ref_el.get_text(" ", strip=True))
        if m:
            ref = m.group(1)
    id_annonce = ref or url

    desc_el = card.select_one(".item__text-block")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Pièces depuis le segment /maison/tN/ de l'URL
    pieces = None
    m_t = re.search(r"/t(\d+)/", href)
    if m_t:
        pieces = int(m_t.group(1))

    surface = parse_surface(titre) or parse_surface(description)

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
        "description": description[:1200],
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
        "agence": AGENCE,
    }


def _parse_loc(text: str) -> tuple[str, str]:
    """'Montaigut-le-Blanc (23320)' → ('Montaigut-le-Blanc', '23320')."""
    cp = ""
    m = re.search(r"\((\d{5})\)", text or "")
    if m:
        cp = m.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text or "").strip()
    return ville, cp


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Parent Immobilier")

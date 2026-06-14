"""scrapers/frenchproperty24.py — 24 French Property (portail anglophone, AWPCP)

Méthode : scrape_simple (httpx) — SSR pur (WordPress + plugin classifieds AWPCP)
URL : /10-2/{cat-id}/{slug}/?offset={N}&results=10
       Les catégories régionales ne mappent PAS strictement un département → on
       parcourt la catégorie « Loire Valley » (405, concentre la zone) ET la
       catégorie globale « for-sale » (15), PUIS POST-FILTRE STRICT sur le code
       postal `CP[:2]` ∈ départements cibles (comme remax/figaro_immo).
Cartes : div.awpcp-listing-excerpt
  - titre + url détail : h4.awpcp-listing-title a[href]
  - ville + CP : div.awpcp-listing-excerpt-content (ex. « Saint-Thibault … – 18300 »)
  - prix : div.awpcp-listing-excerpt-extra (ex. « Price: € 139,000 »)
Particularités :
  - Inventaire modeste en zone cible ; surface rarement en liste (None).
  - Pagination par offset (10/page) ; arrêt sur page vide.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client

BASE = "https://www.24frenchproperty.com"
PAGE_SIZE = 10
MAX_OFFSET = 200       # garde-fou pagination
CATEGORIES = [("405", "loire-valley"), ("15", "for-sale")]


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    results: list[dict] = []
    seen: set[str] = set()

    async with make_client() as client:
        for cat_id, slug in CATEGORIES:
            count = 0
            for offset in range(0, MAX_OFFSET, PAGE_SIZE):
                url = f"{BASE}/10-2/{cat_id}/{slug}/?offset={offset}&results={PAGE_SIZE}"
                r = await get_with_retry(client, url)
                if r is None or r.status_code != 200:
                    break
                cards = BeautifulSoup(r.text, "html.parser").select(
                    "div.awpcp-listing-excerpt")
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
                        continue
                    if bien["id_annonce"] in seen:
                        continue
                    seen.add(bien["id_annonce"])
                    p = bien.get("prix") or 0
                    if prix_max and p and p > prix_max:
                        continue
                    if prix_min and p and p < prix_min:
                        continue
                    results.append(bien)
                    count += 1
                await asyncio.sleep(0.5)
            print(f"[24FP] Catégorie {slug}: {count} annonces (zone)")
            await asyncio.sleep(0.6)

    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("h4.awpcp-listing-title a[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE + href
    titre = link.get_text(" ", strip=True)

    id_m = re.search(r"/real-estate/(\d+)/", href)
    id_annonce = id_m.group(1) if id_m else url

    content_el = card.select_one("div.awpcp-listing-excerpt-content")
    content = content_el.get_text(" ", strip=True) if content_el else ""
    cpm = re.search(r"\b(\d{5})\b", content)
    code_postal = cpm.group(1) if cpm else ""
    # ville : segment avant le tiret/CP
    ville = ""
    mv = re.match(r"\s*([^\d–\-]+?)\s*[–\-]", content)
    if mv:
        ville = mv.group(1).strip()

    extra_el = card.select_one("div.awpcp-listing-excerpt-extra")
    extra = extra_el.get_text(" ", strip=True) if extra_el else ""
    pm = re.search(r"€\s*([\d,\s]+)", extra)
    prix = None
    if pm:
        try:
            prix = float(pm.group(1).replace(",", "").replace(" ", ""))
        except ValueError:
            prix = None

    img = card.select_one("img.awpcp-listing-primary-image-thumbnail")
    photos = []
    if img and img.get("src"):
        src = img["src"]
        photos.append(re.sub(r"-\d+x\d+(\.\w+)$", r"\1", src))

    return {
        "source": "frenchproperty24",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": "maison",
        "description": content[:1200],
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": None,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "24 French Property",
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "24 French Property")

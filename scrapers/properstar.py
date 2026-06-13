"""
scrapers/properstar.py — Properstar.fr (agrégateur international)
Méthode : scrape_simple (httpx) — SSR Angular, pas de Cloudflare
Interface : async def search(criteres: dict) -> list[dict]
Note : 20 annonces par département, post-filtrage Python
"""
import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.properstar.fr"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Department code → URL slug (no accents, no dashes with code)
DEPT_SLUGS = {
    "72": "sarthe",
    "53": "mayenne",
    "49": "maine-et-loire",
    "37": "indre-et-loire",
    "45": "loiret",
    "41": "loir-et-cher",
    "18": "cher",
    "28": "eure-et-loir",
    "36": "indre",
    "89": "yonne",
    "58": "nievre",
}


async def search(criteres: dict) -> list[dict]:
    departements = criteres.get("departements", [])
    prix_max = criteres.get("prix_max", 600000)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 80)

    results = []
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
        for dept in departements:
            dept_str = str(dept).zfill(2)
            slug = DEPT_SLUGS.get(dept_str)
            if not slug:
                continue
            try:
                biens = await _scrape_dept(client, dept_str, slug, prix_max, prix_min, surface_min)
                results.extend(biens)
                print(f"[Properstar] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Properstar] Erreur dept {dept}: {e}")
            await asyncio.sleep(1)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    slug: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    url = f"{BASE_URL}/acheter/maison/{slug}"
    r = await client.get(url)
    r.raise_for_status()
    return _parse_html(r.text, dept, prix_max, prix_min, surface_min)


def _parse_html(
    html: str, dept: str, prix_max: int, prix_min: int, surface_min: int
) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for card in soup.select("article.item-adaptive"):
        try:
            bien = _parse_card(card, dept)
            if not bien:
                continue
            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p > prix_max:
                continue
            if prix_min and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue
            results.append(bien)
        except Exception:
            continue
    return results


def _parse_card(card, dept: str) -> dict | None:
    # Link + ID (both a.advert-vendors-link and a.listing-title share the same href)
    link_el = card.select_one("a[href*='/annonce/']")
    if not link_el:
        return None
    href = link_el.get("href", "")
    url = href if href.startswith("http") else BASE_URL + href
    id_m = re.search(r"/annonce/(\d+)", url)
    ad_id = id_m.group(1) if id_m else url[-20:]

    # Price
    price_el = card.select_one(".listing-price-main, .listing-price")
    if not price_el:
        return None
    prix = _re_float(r"([\d\s\xa0 ]+)\s*€", price_el.get_text(strip=True))
    if not prix:
        return None

    # Title
    title_el = card.select_one("a.listing-title")
    titre_text = title_el.get_text(strip=True) if title_el else "Maison"

    # City
    city_el = card.select_one(".item-location")
    ville = city_el.get_text(strip=True) if city_el else ""

    # Highlights: "Maison • 10 pces • 3 chambres • 1 sdb • 187 m²"
    hl_el = card.select_one(".item-highlights")
    hl_text = hl_el.get_text(" ", strip=True) if hl_el else ""
    surface = _re_float(r"([\d,\.]+)\s*m²", hl_text)
    pieces = _re_int(r"(\d+)\s*p(?:ces|ièces?|ieces?)", hl_text)
    chambres = _re_int(r"(\d+)\s*ch(?:ambres?)?", hl_text)

    # Agency
    agency_el = card.select_one(".user-about")
    agence = agency_el.get_text(strip=True) if agency_el else ""

    # Photos — <picture> tags with source[srcset] containing jpeg URLs
    photos = []
    for pic in card.select("picture.item-picture-img"):
        src_el = pic.select_one("source[srcset*='jpeg'], source[srcset*='jpg']")
        if src_el:
            srcset = src_el.get("srcset", "")
            # take first URL from srcset
            m = re.match(r"([^\s,]+)", srcset)
            if m:
                photos.append(m.group(1))
        if not photos:
            img = pic.select_one("img[src]")
            if img:
                photos.append(img.get("src", ""))

    return {
        "source": "properstar",
        "url": url,
        "id_annonce": ad_id,
        "titre": f"{titre_text} — {ville}"[:150],
        "type_bien": "maison",
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:10],
        "dpe": None,
        "agence": agence[:100],
    }


def _re_float(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text)
    if m:
        try:
            val = m.group(1).replace("\xa0", "").replace(" ", "").replace(" ", "").replace(",", ".")
            return float(val)
        except Exception:
            pass
    return None


def _re_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria
    criteres = load_criteria()
    biens = asyncio.run(search({
        "departements": criteres.departements[:3],
        "prix_max": criteres.prix_max,
        "prix_min": getattr(criteres, "prix_min", 0),
        "surface_min": criteres.surface_min,
    }))
    print(f"\nTotal Properstar: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

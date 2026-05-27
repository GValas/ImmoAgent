"""
scrapers/seloger.py — SeLoger (portail majeur)
Méthode : scrape_simple (httpx) — URL legacy list.htm accessible sans Cloudflare
Interface : async def search(criteres: dict) -> list[dict]
Note : pagination inopérante, ~24 résultats par département, post-filtrage Python
"""
import re
import asyncio

import httpx
from bs4 import BeautifulSoup


BASE_URL = "https://www.seloger.com"
SEARCH_URL = (
    "https://www.seloger.com/list.htm"
    "?types=2&natures=1&projects=2&enterprise=0"
    "&qsVersion=1.0&m=search_refine"
    "&places={places}"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Postal codes prefix map per department
DEPT_POSTAL_CODES = {d: [f"{d}{s:03d}" for s in range(0, 900, 100)] for d in [
    "72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"
]}


async def search(criteres: dict) -> list[dict]:
    departements = criteres.get("departements", [])
    prix_max = criteres.get("prix_max", 600000)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 80)

    results = []
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
        for dept in departements:
            dept_str = str(dept).zfill(2)
            cps = DEPT_POSTAL_CODES.get(dept_str)
            if not cps:
                continue
            try:
                biens = await _scrape_dept(client, dept_str, cps, prix_max, prix_min, surface_min)
                results.extend(biens)
                print(f"[SeLoger] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[SeLoger] Erreur dept {dept}: {e}")
            await asyncio.sleep(1)  # politesse

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    cps: list[str],
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    places = "[" + ",".join(f"{{cp:{cp}}}" for cp in cps) + "]"
    url = SEARCH_URL.format(places=places)
    r = await client.get(url)
    r.raise_for_status()
    return _parse_html(r.text, dept, prix_max, prix_min, surface_min)


def _parse_html(
    html: str, dept: str, prix_max: int, prix_min: int, surface_min: int
) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []

    cards = soup.select("[data-testid='sl.explore.card-container']")
    if not cards:
        cards = soup.select("[class*='cardMode']")

    for card in cards:
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
    # --- Price ---
    price_el = card.select_one("[data-test='sl.price-label']")
    if not price_el:
        return None
    prix = _re_float(r"([\d\s\xa0]+)\s*€", price_el.get_text(strip=True).replace("\xa0", " "))
    if not prix:
        return None

    # --- Title ---
    title_el = card.select_one("[data-test='sl.title']")
    titre = title_el.get_text(strip=True) if title_el else "Maison"

    # --- Tags: pieces, surface, chambres ---
    tags = [li.get_text(strip=True) for li in card.select("[data-test='sl.tagsLine'] li")]
    pieces = None
    surface = None
    chambres = None
    for tag in tags:
        if "pièce" in tag and pieces is None:
            pieces = _re_int(r"(\d+)", tag)
        elif "chambre" in tag and chambres is None:
            chambres = _re_int(r"(\d+)", tag)
        elif "m²" in tag and surface is None:
            surface = _re_float(r"([\d,\.]+)", tag.replace("\xa0", "").replace(" ", ""))

    # --- Address ---
    addr_el = card.select_one("[data-test='sl.address']")
    addr_text = addr_el.get_text(strip=True) if addr_el else ""
    cp_m = re.search(r"\((\d{5})\)", addr_text)
    cp = cp_m.group(1) if cp_m else ""
    ville = re.sub(r"à\s+", "", addr_text)
    ville = re.sub(r"\s*\(?\d{5}\)?\s*$", "", ville).strip()

    # --- Link ---
    link = card.select_one("a[href*='/annonces/achat/maison/']")
    if not link:
        link = card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    url = href if href.startswith("http") else BASE_URL + href

    id_m = re.search(r"/(\d{7,})", url)
    ad_id = id_m.group(1) if id_m else url[-20:]

    # --- Photo ---
    photos = []
    for img in card.select("[data-testid='sl.explore.PhotosContainer'] img[src]"):
        src = img.get("src", "")
        if src.startswith("http"):
            photos.append(src)

    return {
        "source": "seloger",
        "url": url,
        "id_annonce": ad_id,
        "titre": f"{titre} — {addr_text}"[:150],
        "type_bien": "maison",
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:10],
        "dpe": None,
        "agence": "",
    }


def _re_float(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text)
    if m:
        try:
            return float(m.group(1).replace(" ", "").replace(",", "."))
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
        "departements": criteres.departements[:2],
        "prix_max": criteres.prix_max,
        "prix_min": getattr(criteres, "prix_min", 0),
        "surface_min": criteres.surface_min,
    }))
    print(f"\nTotal SeLoger: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

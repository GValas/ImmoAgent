"""
scrapers/nestenn.py — Nestenn (réseau d'agences)
Méthode : Playwright + parsing HTML (div.bien_item)
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from scrapers._base import parse_float as _re_float
from scrapers._base import parse_int as _re_int
from scrapers._base import parse_str_upper as _re_str

BASE_URL = "https://www.nestenn.com"

DEPT_SLUGS = {
    "72": "sarthe-72", "28": "eure-et-loir-28", "45": "loiret-45",
    "89": "yonne-89", "49": "maine-et-loire-49", "37": "indre-et-loire-37",
    "36": "indre-36", "18": "cher-18", "58": "nievre-58",
    "69": "rhone-69", "33": "gironde-33", "34": "herault-34",
    "44": "loire-atlantique-44", "31": "haute-garonne-31",
    "67": "bas-rhin-67", "76": "seine-maritime-76", "59": "nord-59",
    "38": "isere-38", "06": "alpes-maritimes-06", "83": "var-83", "13": "bouches-du-rhone-13",
}


async def search(criteres: dict) -> list[dict]:
    departements = criteres.get("departements", [])
    prix_max = criteres.get("prix_max", 600000)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 80)

    results = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="fr-FR",
        )
        for dept in departements:
            try:
                biens = await _scrape_dept(context, str(dept), prix_min, prix_max, surface_min)
                results.extend(biens)
                print(f"[Nestenn] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Nestenn] Erreur dept {dept}: {e}")
        await browser.close()

    return results


async def _scrape_dept(context, dept: str, prix_min: int, prix_max: int, surface_min: int) -> list[dict]:
    slug = DEPT_SLUGS.get(dept, f"departement-{dept}")
    url = f"{BASE_URL}/vente/maison/{slug}/"

    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector("div.bien_item, .bien-list, .property-list", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(2)
        html = await page.content()
    finally:
        await page.close()

    biens = _parse_html(html, dept)

    # Filtrer par prix et surface
    filtered = []
    for b in biens:
        if b.get("prix") and b["prix"] > prix_max:
            continue
        if prix_min and b.get("prix") and b["prix"] < prix_min:
            continue
        if b.get("surface") and b["surface"] < surface_min:
            continue
        filtered.append(b)

    return filtered


def _parse_html(html: str, dept: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []

    cards = soup.select("div.bien_item")
    if not cards:
        cards = soup.select(".bien-card, .property-item, article[class*='bien']")

    for card in cards:
        try:
            bien = _parse_card(card, dept)
            if bien:
                results.append(bien)
        except Exception:
            continue

    return results


def _parse_card(card, dept: str) -> dict | None:
    link_el = card.select_one("a[href]")
    if not link_el:
        return None
    href = link_el.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + "/" + href.lstrip("/")

    titre_el = card.select_one("h2, h3, h4, .bien-title, .titre, [class*='title']")
    titre = titre_el.get_text(strip=True) if titre_el else link_el.get_text(strip=True)[:100]
    if not titre:
        titre = "Maison à vendre"

    text = card.get_text(" ", strip=True)

    prix = _re_float(r"([\d\s]+)\s*€", text.replace("\xa0", " ").replace(" ", " "))
    surface = _re_float(r"(\d+(?:[.,]\d+)?)\s*m²", text)
    terrain_m = re.search(r"terrain[^\d]{0,20}?(\d[\d\s]*)\s*m²", text, re.IGNORECASE)
    terrain = float(terrain_m.group(1).replace(" ", "")) if terrain_m else None

    pieces = _re_int(r"(\d+)\s*pièces?", text)
    chambres = _re_int(r"(\d+)\s*ch(?:ambres?)?\.?", text)
    dpe = _re_str(r"\bDPE\s*:?\s*([A-G])\b", text)

    ville_el = card.select_one("[class*='ville'], [class*='location'], [class*='city'], [class*='localite']")
    ville = ville_el.get_text(strip=True) if ville_el else ""

    cp_m = re.search(r"\b(\d{5})\b", text)
    cp = cp_m.group(1) if cp_m else ""

    photos = []
    for img in card.select("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy", "")
        if src and "placeholder" not in src.lower() and ("nestenn" in src or src.startswith("http")):
            if not src.startswith("http"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:10]

    id_m = re.search(r"ref[-_]?(\d+)|/(\d{6,})", url)
    ad_id = id_m.group(1) or id_m.group(2) if id_m else url[-20:]

    return {
        "source": "nestenn",
        "url": url,
        "id_annonce": str(ad_id),
        "titre": titre[:150],
        "type_bien": "maison",
        "description": text[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Nestenn",
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria
    criteres = load_criteria()
    biens = asyncio.run(search({
        "departements": criteres.departements[:2],
        "prix_max": criteres.prix_max,
        "prix_min": criteres.prix_min,
        "surface_min": criteres.surface_min,
    }))
    print(f"\nTotal Nestenn: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

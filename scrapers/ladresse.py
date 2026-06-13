"""
scrapers/ladresse.py — L'Adresse (réseau d'agences)
Méthode : Playwright + parsing HTML (a.bien)
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

BASE_URL = "https://www.ladresse.com"

DEPT_CODES = {
    "72": "72", "28": "28", "45": "45", "89": "89", "49": "49",
    "37": "37", "36": "36", "18": "18", "58": "58",
    "69": "69", "33": "33", "34": "34", "44": "44", "31": "31",
    "67": "67", "76": "76", "59": "59", "38": "38",
    "06": "06", "83": "83", "13": "13", "84": "84",
    "75": "75", "92": "92", "93": "93", "94": "94",
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
                print(f"[LAdresse] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[LAdresse] Erreur dept {dept}: {e}")
        await browser.close()

    return results


async def _scrape_dept(context, dept: str, prix_min: int, prix_max: int, surface_min: int) -> list[dict]:
    params = [
        "typeAnnonce=1",
        "typeBien=maison",
        f"departement={dept}",
    ]
    if prix_max:
        params.append(f"prixMax={prix_max}")
    if prix_min:
        params.append(f"prixMin={prix_min}")
    if surface_min:
        params.append(f"surfaceMin={surface_min}")

    url = f"{BASE_URL}/annonces?{'&'.join(params)}"

    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector("a.bien", timeout=8000)
        except Exception:
            pass
        await asyncio.sleep(2)
        html = await page.content()
    finally:
        await page.close()

    return _parse_html(html, dept)


def _parse_html(html: str, dept: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen_ids = set()

    biens = soup.select("a.bien")
    for card in biens:
        try:
            bien = _parse_card(card, dept)
            if bien and bien["id_annonce"] not in seen_ids:
                seen_ids.add(bien["id_annonce"])
                results.append(bien)
        except Exception:
            continue

    return results


def _parse_card(card, dept: str) -> dict | None:
    ad_id = str(card.get("data-id", ""))
    href = card.get("href", "")
    if not href:
        return None
    url = BASE_URL + href if href.startswith("/") else href

    # Photo
    img = card.select_one("img")
    photos = []
    if img:
        src = img.get("src") or img.get("data-src", "")
        if src and src.startswith("http"):
            photos = [src]

    # City from span.bien-geo
    geo_el = card.select_one(".bien-geo")
    ville_raw = geo_el.get_text(strip=True) if geo_el else ""
    # "Le Mans (72)" → ville="Le Mans", cp="72000"
    city_m = re.match(r"(.+?)\s*\((\d{5})\)", ville_raw)
    if city_m:
        ville = city_m.group(1).strip()
        cp = city_m.group(2)
    else:
        cp_m = re.search(r"\b(\d{5})\b", ville_raw)
        ville = re.sub(r"\s*\(\d+\)\s*", "", ville_raw).strip()
        cp = cp_m.group(1) if cp_m else ""

    # Price
    prix_el = card.select_one(".bien-prix")
    prix_text = prix_el.get_text(strip=True) if prix_el else ""
    prix = _re_float(r"([\d\s]+)\s*€", prix_text.replace("\xa0", " ").replace(" ", " "))

    # Type
    type_el = card.select_one(".bien-type")

    # Rooms/surface from span.highlight
    hl_el = card.select_one("span.highlight")
    hl_text = hl_el.get_text(" ", strip=True) if hl_el else ""

    pieces_m = re.search(r"(\d+)\s*pièces?", hl_text, re.IGNORECASE)
    pieces = int(pieces_m.group(1)) if pieces_m else None

    chb_m = re.search(r"(\d+)\s*chambres?", hl_text, re.IGNORECASE)
    chambres = int(chb_m.group(1)) if chb_m else None

    surf_m = re.search(r"([\d.,]+)\s*m²", hl_text)
    surface = float(surf_m.group(1).replace(",", ".")) if surf_m else None

    # Description (text after highlight)
    desc_el = card.select_one(".bien-description")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Terrain from description keywords
    terrain = None
    terrain_m = re.search(r"terrain[^\d]{0,20}?(\d[\d\s]*)\s*m²", description, re.IGNORECASE)
    if terrain_m:
        terrain = float(terrain_m.group(1).replace(" ", ""))

    titre_img = img.get("alt", "") if img else ""
    titre = titre_img or f"Maison {pieces or ''} pièces {ville}".strip()

    return {
        "source": "ladresse",
        "url": url,
        "id_annonce": ad_id or url[-12:],
        "titre": titre[:150],
        "type_bien": "maison",
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "L'Adresse",
    }


def _re_float(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text)
    if m:
        try:
            return float(m.group(1).replace(" ", "").replace(",", "."))
        except Exception:
            pass
    return None


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
    print(f"\nTotal L'Adresse: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

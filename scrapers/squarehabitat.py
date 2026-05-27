"""
scrapers/squarehabitat.py — Square Habitat (réseau Crédit Agricole)
Méthode : Playwright — Angular SSR
Interface : async def search(criteres: dict) -> list[dict]
"""
import re
import asyncio

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


BASE_URL = "https://www.squarehabitat.fr"

# (region, dept-slug) — URL: /annonces/achat/bien/maison/immobilier/{region}/{slug}
DEPT_SLUGS = {
    "72": ("pays-de-la-loire",        "sarthe-72"),
    "53": ("pays-de-la-loire",        "mayenne-53"),
    "49": ("pays-de-la-loire",        "maine-et-loire-49"),
    "37": ("centre-val-de-loire",     "indre-et-loire-37"),
    "45": ("centre-val-de-loire",     "loiret-45"),
    "41": ("centre-val-de-loire",     "loir-et-cher-41"),
    "18": ("centre-val-de-loire",     "cher-18"),
    "28": ("centre-val-de-loire",     "eure-et-loir-28"),
    "36": ("centre-val-de-loire",     "indre-36"),
    "89": ("bourgogne-franche-comte", "yonne-89"),
    "58": ("bourgogne-franche-comte", "nievre-58"),
}


async def search(criteres: dict) -> list[dict]:
    departements = criteres.get("departements", [])
    prix_max = criteres.get("prix_max", 600000)
    prix_min = criteres.get("prix_min", 0)

    results = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="fr-FR",
        )
        for dept in departements:
            dept_str = str(dept).zfill(2)
            mapping = DEPT_SLUGS.get(dept_str)
            if not mapping:
                continue
            try:
                biens = await _scrape_dept(context, dept_str, mapping, prix_max, prix_min)
                results.extend(biens)
                print(f"[SquareHabitat] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[SquareHabitat] Erreur dept {dept}: {e}")
        await browser.close()

    return results


async def _scrape_dept(context, dept: str, mapping: tuple, prix_max: int, prix_min: int) -> list[dict]:
    region, slug = mapping
    url = f"{BASE_URL}/annonces/achat/bien/maison/immobilier/{region}/{slug}"
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector("[class*='card-btm']", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(2)
        html = await page.content()
    finally:
        await page.close()
    return _parse_html(html, dept, prix_max, prix_min)


def _parse_html(html: str, dept: str, prix_max: int, prix_min: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []

    cards = soup.select("[class*='card-btm']")
    for card in cards:
        try:
            bien = _parse_card(card, dept)
            if bien:
                # Post-filter price (site has no price param in URL)
                p = bien.get("prix") or 0
                if prix_max and p > prix_max:
                    continue
                if prix_min and p < prix_min:
                    continue
                results.append(bien)
        except Exception:
            continue
    return results


def _parse_card(card, dept: str) -> dict | None:
    # Listing link
    link = card.select_one("a[href*='/annonces/biens/achat-ancien/maison/']")
    if not link:
        return None
    href = link.get("href", "")
    url = href if href.startswith("http") else BASE_URL + href

    # Title + pieces
    titre = link.get_text(strip=True)
    pieces = _re_int(r"(\d+)\s*pièces?", titre)

    # City + CP
    city_el = card.select_one("[class*='card-btm-title']")
    city_text = city_el.get_text(" ", strip=True) if city_el else ""
    cp_m = re.search(r"\((\d{5})\)", city_text)
    cp = cp_m.group(1) if cp_m else ""
    ville = re.sub(r"\s*\(?\d{5}\)?\s*", "", city_text).strip()

    # Price — "Au prix de 179 500 €" or bare "179 500 €"
    card_text = card.get_text(" ", strip=True).replace("\xa0", " ")
    prix = _re_float(r"([\d ]{5,})\s*€", card_text)

    # ID from URL
    id_m = re.search(r"/([a-f0-9\-]{30,})", url)
    ad_id = id_m.group(1) if id_m else url[-20:]

    if not prix:
        return None

    return {
        "source": "squarehabitat",
        "url": url,
        "id_annonce": ad_id,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": None,        # non disponible sur la page liste
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": [],
        "dpe": None,
        "agence": "Square Habitat",
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
    print(f"\nTotal SquareHabitat: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['ville']}")

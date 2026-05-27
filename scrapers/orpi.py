"""
scrapers/orpi.py — Orpi (réseau d'agences)
Méthode : Playwright + parsing HTML (article.c-estate-thumb)
Interface : async def search(criteres: dict) -> list[dict]
"""
import re
import asyncio

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


BASE_URL = "https://www.orpi.com"

DEPT_IDS = {
    "72": "department_72", "28": "department_28", "45": "department_45",
    "89": "department_89", "49": "department_49", "37": "department_37",
    "36": "department_36", "18": "department_18", "58": "department_58",
    "69": "department_69", "33": "department_33", "34": "department_34",
    "44": "department_44", "31": "department_31", "67": "department_67",
    "76": "department_76", "59": "department_59", "38": "department_38",
    "06": "department_06", "83": "department_83", "13": "department_13",
}


async def search(criteres: dict) -> list[dict]:
    departements = criteres.get("departements", [])
    prix_max = criteres.get("prix_max", 600000)
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
                biens = await _scrape_dept(context, str(dept), prix_max, surface_min)
                results.extend(biens)
                print(f"[Orpi] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Orpi] Erreur dept {dept}: {e}")
        await browser.close()

    return results


async def _scrape_dept(context, dept: str, prix_max: int, surface_min: int) -> list[dict]:
    dept_id = DEPT_IDS.get(dept, f"department_{dept}")
    url = (
        f"{BASE_URL}/recherche/buy"
        f"?types[]=house"
        f"&locationIds[]={dept_id}"
        f"&priceMax={prix_max}"
        f"&areaMin={surface_min}"
    )

    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector("article.c-estate-thumb, .c-estate-thumb, .property-card", timeout=10000)
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

    cards = soup.select("article.c-estate-thumb")
    if not cards:
        cards = soup.select(".c-estate-thumb")
    if not cards:
        cards = soup.select("article[class*='estate']")

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
    url = href if href.startswith("http") else BASE_URL + href

    titre_el = card.select_one("h2, h3, .c-estate-thumb__title, [class*='title']")
    titre = titre_el.get_text(strip=True) if titre_el else link_el.get_text(strip=True)[:100]

    text = card.get_text(" ", strip=True)

    prix_el = card.select_one("[class*='price'], [class*='prix']")
    if prix_el:
        prix = _re_float(r"([\d\s]+)\s*€", prix_el.get_text().replace("\xa0", " "))
    else:
        prix = _re_float(r"([\d\s]{4,})\s*€", text.replace("\xa0", " "))

    surface = _re_float(r"(\d+(?:[.,]\d+)?)\s*m²\s*(?:hab|int|liv)?", text)
    terrain_m = re.search(r"terrain[^\d]{0,20}?(\d[\d\s]*)\s*m²", text, re.IGNORECASE)
    terrain = float(terrain_m.group(1).replace(" ", "")) if terrain_m else None

    pieces = _re_int(r"(\d+)\s*pièces?", text)
    chambres = _re_int(r"(\d+)\s*ch(?:ambres?)?\.?", text)
    dpe = _re_str(r"\bDPE\s*:?\s*([A-G])\b", text)

    ville_el = card.select_one("[class*='location'], [class*='city'], [class*='ville'], [class*='address']")
    ville = ville_el.get_text(strip=True) if ville_el else _extract_city(text)

    cp_m = re.search(r"\b(\d{5})\b", text)
    cp = cp_m.group(1) if cp_m else ""

    photos = []
    for img in card.select("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy", "")
        if src and "placeholder" not in src.lower() and src.startswith("http"):
            photos.append(src)
    photos = photos[:10]

    ad_id = card.get("data-id", "") or card.get("id", "")
    if not ad_id:
        id_m = re.search(r"/annonce[^/]*?-(\d+)", url)
        ad_id = id_m.group(1) if id_m else url[-20:]

    desc_el = card.select_one("[class*='desc'], [class*='excerpt'], [class*='text']")
    description = desc_el.get_text(strip=True)[:1200] if desc_el else ""

    return {
        "source": "orpi",
        "url": url,
        "id_annonce": str(ad_id),
        "titre": titre[:150],
        "type_bien": "maison",
        "description": description,
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
        "agence": "Orpi",
    }


def _extract_city(text: str) -> str:
    m = re.search(r"\b([A-ZÉÈÊËÀÂÙÛÎÏÔÇ][a-zéèêëàâùûîïôç]+(?:[-\s][A-ZÉÈÊËÀÂÙÛÎÏÔÇ][a-zéèêëàâùûîïôç]+)*)\b", text)
    return m.group(1) if m else ""


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


def _re_str(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).upper() if m else None


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria
    criteres = load_criteria()
    biens = asyncio.run(search({
        "departements": criteres.departements[:2],
        "prix_max": criteres.prix_max,
        "surface_min": criteres.surface_min,
    }))
    print(f"\nTotal Orpi: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

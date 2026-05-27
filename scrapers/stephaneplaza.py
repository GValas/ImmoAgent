"""
scrapers/stephaneplaza.py — Stéphane Plaza Immobilier (franchise nationale)
Méthode : Playwright + parsing HTML
Interface : async def search(criteres: dict) -> list[dict]
"""
import re
import asyncio

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


BASE_URL = "https://www.stephaneplaza.com"

MAX_PAGES = 5


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
                print(f"[StéphanePlaza] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[StéphanePlaza] Erreur dept {dept}: {e}")
        await browser.close()

    return results


async def _scrape_dept(context, dept: str, prix_min: int, prix_max: int, surface_min: int) -> list[dict]:
    biens = []
    seen_ids = set()

    for page_num in range(1, MAX_PAGES + 1):
        url = (
            f"{BASE_URL}/nos-biens/resultats/"
            f"?transaction=1&type[]=1&dept[]={dept}"
            f"&prixmax={prix_max}&surfmin={surface_min}"
            + (f"&prixmin={prix_min}" if prix_min else "")
            + (f"&page={page_num}" if page_num > 1 else "")
        )

        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector(
                    "article, .property-card, .bien-card, .annonce-item, .c-property",
                    timeout=10000
                )
            except Exception:
                pass
            await asyncio.sleep(2)
            html = await page.content()
        finally:
            await page.close()

        cards, total = _parse_html(html, dept)
        new_found = 0
        for b in cards:
            if b["id_annonce"] not in seen_ids:
                seen_ids.add(b["id_annonce"])
                biens.append(b)
                new_found += 1

        if not new_found:
            break

    return biens


def _parse_html(html: str, dept: str) -> tuple[list[dict], int]:
    soup = BeautifulSoup(html, "html.parser")

    cards = (
        soup.select("article.property-card")
        or soup.select("article.bien-card")
        or soup.select(".annonce-item")
        or soup.select(".c-property")
        or soup.select("article[class*='property']")
        or soup.select("article[class*='bien']")
        or soup.select("article")
    )

    total = 0
    total_m = re.search(r"(\d+)\s*(?:annonces?|biens?|résultats?)", soup.get_text(" "), re.IGNORECASE)
    if total_m:
        total = int(total_m.group(1))

    results = []
    seen_ids = set()
    for card in cards:
        try:
            bien = _parse_card(card, dept)
            if bien and bien["id_annonce"] not in seen_ids:
                seen_ids.add(bien["id_annonce"])
                results.append(bien)
        except Exception:
            continue

    return results, total


def _parse_card(card, dept: str) -> dict | None:
    link_el = card.select_one("a[href]")
    if not link_el:
        return None
    href = link_el.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    id_m = re.search(r"[-/](\d{5,})", href)
    if not id_m:
        id_m = re.search(r"(\d{5,})", href)
    ad_id = id_m.group(1) if id_m else href[-12:]

    text = card.get_text(" ", strip=True)
    normalized = text.replace("\xa0", " ").replace(" ", " ")

    prix = _re_float(r"([\d\s]{4,})\s*€", normalized)
    surface = _re_float(r"(\d+(?:[.,]\d+)?)\s*m²", text)
    terrain_m = re.search(r"[Tt]errain\s+([\d\s]+)\s*m²", normalized)
    terrain = float(terrain_m.group(1).replace(" ", "")) if terrain_m else None
    pieces = _re_int(r"(\d+)\s*pièces?", text)
    chambres = _re_int(r"(\d+)\s*ch(?:ambres?)?", text)
    dpe = _re_str(r"\bDPE\s*:?\s*([A-G])\b", text)

    city_m = re.search(r"([A-ZÉÈÊËÀÂÙÛÎÏÔÇa-zéèêëàâùûîïôç][^\d(]{2,30})\s*\((\d{5})\)", text)
    ville = city_m.group(1).strip() if city_m else ""
    cp = city_m.group(2) if city_m else ""

    titre_el = card.select_one("h2, h3, [class*='title'], [class*='titre']")
    titre = titre_el.get_text(strip=True)[:150] if titre_el else f"Maison {pieces or ''} pièces {ville}".strip()

    photos = []
    for img in card.select("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy", "")
        if src and "http" in src and "placeholder" not in src.lower():
            if any(ext in src.lower() for ext in [".jpg", ".jpeg", ".webp", ".png"]):
                photos.append(src)
    photos = list(dict.fromkeys(photos))[:10]

    desc_el = card.select_one("[class*='desc'], [class*='text'], [class*='excerpt']")
    description = desc_el.get_text(strip=True)[:1200] if desc_el else ""

    return {
        "source": "stephaneplaza",
        "url": url,
        "id_annonce": str(ad_id),
        "titre": titre,
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
        "agence": "Stéphane Plaza Immobilier",
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
        "prix_min": criteres.prix_min,
        "surface_min": criteres.surface_min,
    }))
    print(f"\nTotal Stéphane Plaza: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

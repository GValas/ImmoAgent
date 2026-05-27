"""
scrapers/pap.py — PAP (De Particulier à Particulier)
Méthode : Playwright (Cloudflare bypass) + scraping HTML
Interface : async def search(criteres: dict) -> list[dict]
"""
import re
import asyncio

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


BASE_URL = "https://www.pap.fr"

# ID famille PAP : 4 = maison/villa/ferme, 3 = appartement
FAMILLE_MAISON = "4"


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
            except Exception as e:
                print(f"[PAP] Erreur dept {dept}: {e}")
        await browser.close()

    return results


async def _scrape_dept(context, dept: str, prix_min: int, prix_max: int, surface_min: int) -> list[dict]:
    page = await context.new_page()
    try:
        params = [
            f"locat-departement[]={dept}",
            f"famille={FAMILLE_MAISON}",
            f"prix-max={prix_max}",
            f"surface-min={surface_min}",
        ]
        if prix_min:
            params.append(f"prix-min={prix_min}")

        url = f"{BASE_URL}/annonce/ventes-maison?{'&'.join(params)}"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Attendre le chargement des annonces
        try:
            await page.wait_for_selector("article, .search-list-item, li.item", timeout=8000)
        except Exception:
            pass

        html = await page.content()
        return _parse_page(html, dept)
    finally:
        await page.close()


def _parse_page(html: str, dept: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []

    # PAP utilise des articles ou li.item pour les annonces
    selectors = [
        "article.item-listing",
        "article.search-list-item",
        "li.item",
        "article[data-id]",
        ".item-list article",
    ]
    items = []
    for sel in selectors:
        items = soup.select(sel)
        if items:
            break

    if not items:
        # Fallback: articles avec prix visible
        items = [
            el for el in soup.select("article, li")
            if re.search(r"\d{3}[\s\xa0]\d{3}", el.get_text())
        ]

    for item in items:
        try:
            bien = _parse_item(item, dept)
            if bien:
                results.append(bien)
        except Exception:
            continue

    return results


def _parse_item(item, dept: str) -> dict | None:
    link = item.select_one("a[href]")
    if not link:
        return None
    url = link.get("href", "")
    if url.startswith("/"):
        url = BASE_URL + url
    if not url:
        return None

    text = item.get_text(" ", strip=True)

    title_el = item.select_one("h2, h3, .item-title, .title, .heading")
    titre = title_el.get_text(strip=True) if title_el else link.get_text(strip=True)[:100]

    # Ville
    city_el = item.select_one(".item-location, .location, .localisation, .city")
    ville = city_el.get_text(strip=True) if city_el else ""

    # Photos
    photos = []
    for img in item.select("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy", "")
        if src and "placeholder" not in src.lower() and not src.endswith(".gif"):
            if src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)

    ad_id = item.get("data-id", "") or item.get("id", "")

    return {
        "source": "pap",
        "url": url,
        "id_annonce": str(ad_id) if ad_id else url,
        "titre": titre,
        "type_bien": "maison",
        "description": text[:1200],
        "departement": dept,
        "ville": ville,
        "code_postal": _extract_cp(text),
        "surface": _extract_surface(text),
        "surface_terrain": _extract_terrain(text),
        "pieces": _extract_pieces(text),
        "prix": _extract_prix(text),
        "photos": photos[:10],
        "dpe": _extract_dpe(text),
    }


def _extract_prix(text: str) -> float | None:
    text = text.replace("\xa0", " ").replace(" ", " ")
    m = re.search(r"(\d[\d ]{3,8})\s*€", text)
    if m:
        try:
            return float(m.group(1).replace(" ", ""))
        except Exception:
            pass
    return None


def _extract_surface(text: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", text)
    if m:
        return float(m.group(1).replace(",", "."))
    return None


def _extract_terrain(text: str) -> float | None:
    m = re.search(r"terrain[^.]{0,40}?(\d[\d\s]*)\s*m²", text, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(" ", ""))
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*ha\b", text, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", ".")) * 10000
    return None


def _extract_pieces(text: str) -> int | None:
    m = re.search(r"(\d+)\s*pièces?", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _extract_cp(text: str) -> str:
    m = re.search(r"\b(\d{5})\b", text)
    return m.group(1) if m else ""


def _extract_dpe(text: str) -> str | None:
    m = re.search(r"\bDPE\s*:?\s*([A-G])\b", text, re.IGNORECASE)
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
    print(f"\nTotal PAP: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

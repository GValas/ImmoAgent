"""
scrapers/logic_immo.py — Logic-Immo (SeLoger Group)
Méthode : Playwright + interception réseau API JSON
Interface : async def search(criteres: dict) -> list[dict]
"""
import re
import json
import asyncio

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


BASE_URL = "https://www.logic-immo.com"

# groupprptypesids : 1=maison, 2=villa, 6=propriété, 7=manoir, 13=fermette
DEPT_SLUGS = {
    "72": "sarthe-72",
    "28": "eure-et-loir-28",
    "45": "loiret-45",
    "89": "yonne-89",
    "49": "maine-et-loire-49",
    "37": "indre-et-loire-37",
    "36": "indre-36",
    "18": "cher-18",
    "58": "nievre-58",
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
            except Exception as e:
                print(f"[LogicImmo] Erreur dept {dept}: {e}")
        await browser.close()

    return results


async def _scrape_dept(context, dept: str, prix_max: int, surface_min: int) -> list[dict]:
    slug = DEPT_SLUGS.get(dept, dept)
    url = (
        f"{BASE_URL}/vente-immobilier-{slug},1_0"
        f"/options/groupprptypesids=1,2,6,7,13"
        f"/budgetmax={prix_max}"
        f"/surfacemin={surface_min}"
    )

    intercepted: list[dict] = []

    page = await context.new_page()
    try:
        async def handle_response(response):
            url_r = response.url
            # Intercepter les API JSON de Logic-Immo
            if response.status == 200 and any(
                k in url_r for k in ("annonces-vente", "listing", "search", "offers")
            ) and "json" in response.headers.get("content-type", ""):
                try:
                    data = await response.json()
                    ads = _find_ads(data)
                    for ad in ads:
                        bien = _parse_ad(ad, dept)
                        if bien:
                            intercepted.append(bien)
                except Exception:
                    pass

        page.on("response", handle_response)

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector("article, .offer-list-item, .list-result", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(2)

        # Si interception vide, parser le HTML rendu
        if not intercepted:
            html = await page.content()
            intercepted.extend(_parse_html(html, dept))

    finally:
        await page.close()

    return intercepted


def _find_ads(obj, depth: int = 0) -> list:
    """Cherche récursivement une liste d'annonces dans le JSON."""
    if depth > 6:
        return []
    if isinstance(obj, list) and len(obj) > 0 and isinstance(obj[0], dict):
        first = obj[0]
        if any(k in first for k in ("price", "prix", "surfaceArea", "surface", "id", "adId")):
            return obj
    if isinstance(obj, dict):
        for v in obj.values():
            result = _find_ads(v, depth + 1)
            if result:
                return result
    return []


def _parse_ad(ad: dict, dept: str) -> dict | None:
    try:
        prix = ad.get("price") or ad.get("prix")
        surface = ad.get("surfaceArea") or ad.get("surface")
        url = ad.get("url") or ad.get("link") or ad.get("adUrl") or ""
        if url and url.startswith("/"):
            url = BASE_URL + url

        photos_raw = ad.get("photos") or ad.get("images") or []
        photos = []
        for p in photos_raw[:10]:
            if isinstance(p, str):
                photos.append(p)
            elif isinstance(p, dict):
                photos.append(p.get("url") or p.get("src", ""))

        return {
            "source": "logic_immo",
            "url": url,
            "id_annonce": str(ad.get("id") or ad.get("adId", "")),
            "titre": ad.get("title") or ad.get("titre", ""),
            "type_bien": "maison",
            "description": str(ad.get("description", ""))[:1200],
            "departement": dept,
            "ville": ad.get("city") or ad.get("ville", ""),
            "code_postal": str(ad.get("postalCode") or ad.get("codePostal", "")),
            "surface": float(surface) if surface else None,
            "surface_terrain": ad.get("landSurface") or ad.get("surfaceTerrain"),
            "pieces": ad.get("rooms") or ad.get("roomsQuantity") or ad.get("pieces"),
            "chambres": ad.get("bedrooms") or ad.get("bedroomsQuantity"),
            "prix": float(prix) if prix else None,
            "photos": [p for p in photos if p],
            "dpe": ad.get("dpe") or ad.get("energyRating") or ad.get("energyClassification"),
            "agence": ad.get("agencyName") or ad.get("agence", ""),
        }
    except Exception:
        return None


def _parse_html(html: str, dept: str) -> list[dict]:
    """Fallback scraping HTML rendu par Playwright."""
    soup = BeautifulSoup(html, "html.parser")
    results = []

    selectors = [
        "article.offer-list-item",
        ".annonce-item",
        "li[data-id]",
        "div[data-id]",
        ".property-card",
        "article",
    ]
    items = []
    for sel in selectors:
        items = soup.select(sel)
        if len(items) > 2:
            break

    for item in items:
        try:
            link = item.select_one("a[href*='vente'], a[href*='annonce']")
            if not link:
                continue
            url = link.get("href", "")
            if url.startswith("/"):
                url = BASE_URL + url

            text = item.get_text(" ", strip=True)
            prix = _re_float(r"(\d[\d\s]*)\s*€", text.replace("\xa0", " ").replace(" ", " "))
            surface = _re_float(r"(\d+(?:[.,]\d+)?)\s*m²", text)

            results.append({
                "source": "logic_immo",
                "url": url,
                "id_annonce": item.get("data-id", ""),
                "titre": (item.select_one("h2, h3, .title") or link).get_text(strip=True)[:100],
                "type_bien": "maison",
                "description": "",
                "departement": dept,
                "ville": "",
                "code_postal": "",
                "surface": surface,
                "surface_terrain": None,
                "pieces": _re_int(r"(\d+)\s*pièces?", text),
                "prix": prix,
                "photos": [img.get("src") for img in item.select("img") if img.get("src")][:5],
                "dpe": _re_str(r"\bDPE\s*:?\s*([A-G])\b", text),
            })
        except Exception:
            continue

    return results


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
    print(f"\nTotal LogicImmo: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

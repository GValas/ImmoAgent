"""
scrapers/safti.py — SAFTI (réseau de mandataires)
Méthode : Playwright + parsing HTML (article.tw-group)
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from scrapers._base import parse_float as _re_float
from scrapers._base import parse_str_upper as _re_str

BASE_URL = "https://www.safti.fr"

DEPT_SLUGS = {
    "72": "sarthe-72", "28": "eure-et-loir-28", "45": "loiret-45",
    "89": "yonne-89", "49": "maine-et-loire-49", "37": "indre-et-loire-37",
    "36": "indre-36", "18": "cher-18", "58": "nievre-58",
    "69": "rhone-69", "33": "gironde-33", "34": "herault-34",
    "44": "loire-atlantique-44", "31": "haute-garonne-31",
    "67": "bas-rhin-67", "76": "seine-maritime-76", "59": "nord-59",
    "38": "isere-38", "06": "alpes-maritimes-06", "83": "var-83", "13": "bouches-du-rhone-13",
    "75": "paris-75", "92": "hauts-de-seine-92", "93": "seine-saint-denis-93", "94": "val-de-marne-94",
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
                print(f"[Safti] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Safti] Erreur dept {dept}: {e}")
        await browser.close()

    return results


async def _scrape_dept(context, dept: str, prix_min: int, prix_max: int, surface_min: int) -> list[dict]:
    slug = DEPT_SLUGS.get(dept, f"departement-{dept}")
    url = f"{BASE_URL}/annonces/vente/maison/{slug}/"

    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector("article", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(2)
        html = await page.content()
    finally:
        await page.close()

    biens = _parse_html(html, dept)

    filtered = []
    for b in biens:
        if b.get("prix") and prix_max and b["prix"] > prix_max:
            continue
        if prix_min and b.get("prix") and b["prix"] < prix_min:
            continue
        if b.get("surface") and surface_min and b["surface"] < surface_min:
            continue
        filtered.append(b)

    return filtered


def _parse_html(html: str, dept: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []

    # SAFTI uses TailwindCSS: article.tw-group cards, but shows both mobile (lg:tw-hidden) and desktop
    # Select all articles and deduplicate by id_annonce
    all_articles = soup.select("article")
    seen_ids = set()

    for card in all_articles:
        try:
            bien = _parse_card(card, dept)
            if bien and bien["id_annonce"] not in seen_ids:
                seen_ids.add(bien["id_annonce"])
                results.append(bien)
        except Exception:
            continue

    return results


def _parse_card(card, dept: str) -> dict | None:
    text = card.get_text(" ", strip=True)

    link_el = card.select_one("a[href]")
    if not link_el:
        return None
    href = link_el.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Extract annonce id from URL: /annonces/achat/maison/ville-cp/ID
    id_m = re.search(r"/(\d{5,})\s*$", href.rstrip("/"))
    if not id_m:
        id_m = re.search(r"/(\d{5,})", href)
    ad_id = id_m.group(1) if id_m else href[-12:]

    # Title from text: "Maison - N pièces - Xm²"
    titre_m = re.search(r"(Maison[^€\n]{3,60})", text)
    titre = titre_m.group(1).strip() if titre_m else "Maison à vendre"

    # Price: "142 000 €"
    prix = _re_float(r"([\d\s]+)\s*€", text.replace("\xa0", " ").replace(" ", " "))

    # Surface from title or text
    surface_m = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", titre or text)
    surface = float(surface_m.group(1).replace(",", ".")) if surface_m else None

    # Terrain
    terrain_m = re.search(r"[Tt]errain\s+([\d\s]+)\s*m²", text.replace(" ", " "))
    terrain = float(terrain_m.group(1).replace(" ", "")) if terrain_m else None

    # Pieces
    pieces_m = re.search(r"(\d+)\s*pièces?", text, re.IGNORECASE)
    if not pieces_m:
        pieces_m = re.search(r"(\d+)\s*p\.", text)
    pieces = int(pieces_m.group(1)) if pieces_m else None

    # Chambres from "bedroom N" or "N chambres"
    chb_m = re.search(r"bedroom\s+(\d+)|(\d+)\s+chambres?", text, re.IGNORECASE)
    chambres = int(chb_m.group(1) or chb_m.group(2)) if chb_m else None

    # City and CP: "Allonnes (72700)"
    city_m = re.search(r"([A-ZÉÈÊËÀÂÙÛÎÏÔÇa-zéèêëàâùûîïôç][^\d(]{2,30})\s*\((\d{5})\)", text)
    ville = city_m.group(1).strip() if city_m else ""
    cp = city_m.group(2) if city_m else ""

    # DPE
    dpe = _re_str(r"\bDPE\s*:?\s*([A-G])\b", text)

    # Photos - property photos are in carousel, img tags
    photos = []
    for img in card.select("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and "cdn.safti.fr" in src and "agent-photo" not in src:
            photos.append(src)
        elif src and "safti" in src and src.startswith("http"):
            photos.append(src)
    photos = list(dict.fromkeys(photos))[:10]  # deduplicate

    return {
        "source": "safti",
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
        "agence": "SAFTI",
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
    print(f"\nTotal SAFTI: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

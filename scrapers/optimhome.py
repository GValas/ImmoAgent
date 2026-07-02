"""
scrapers/optimhome.py — Optimhome (réseau de mandataires)
Méthode : Playwright + parsing HTML (.card.property-card)
URL : /fr/immobilier/vente/maison/{slug}/ (ex: sarthe)
Lien annonce via data-href (pas href) + data-* pour prix/ville/cp
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from scrapers._base import parse_float as _re_float
from scrapers._base import parse_int as _re_int
from scrapers._base import parse_str_upper as _re_str

BASE_URL = "https://www.optimhome.com"

DEPT_SLUGS = {
    "72": "sarthe", "28": "eure-et-loir", "45": "loiret",
    "89": "yonne", "49": "maine-et-loire", "37": "indre-et-loire",
    "36": "indre", "18": "cher", "58": "nievre",
    "69": "rhone", "33": "gironde", "34": "herault",
    "44": "loire-atlantique", "31": "haute-garonne",
    "67": "bas-rhin", "76": "seine-maritime", "59": "nord",
    "38": "isere", "06": "alpes-maritimes", "83": "var", "13": "bouches-du-rhone",
    "75": "paris", "92": "hauts-de-seine", "93": "seine-saint-denis", "94": "val-de-marne",
    "84": "vaucluse", "26": "drome", "30": "gard", "11": "aude",
    "63": "puy-de-dome", "03": "allier", "23": "creuse",
    "41": "loir-et-cher", "61": "orne", "53": "mayenne",
    "86": "vienne", "79": "deux-sevres", "85": "vendee", "87": "haute-vienne",
}

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
                print(f"[Optimhome] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Optimhome] Erreur dept {dept}: {e}")
        await browser.close()

    return results


async def _scrape_dept(context, dept: str, prix_min: int, prix_max: int, surface_min: int) -> list[dict]:
    slug = DEPT_SLUGS.get(dept)
    if not slug:
        return []

    biens = []
    seen_ids = set()

    for page_num in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/fr/immobilier/vente/maison/{slug}/"
        if page_num > 1:
            url += f"?page={page_num}"

        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector(".property-card, .card.property-card", timeout=10000)
            except Exception:
                pass
            await asyncio.sleep(3)
            html = await page.content()
        finally:
            await page.close()

        cards = _parse_html(html, dept)
        if not cards:
            break

        new_found = 0
        for b in cards:
            if b["id_annonce"] in seen_ids:
                continue
            if b.get("prix") and prix_max and b["prix"] > prix_max:
                continue
            if prix_min and b.get("prix") and b["prix"] < prix_min:
                continue
            if b.get("surface") and surface_min and b["surface"] < surface_min:
                continue
            seen_ids.add(b["id_annonce"])
            biens.append(b)
            new_found += 1

        if new_found == 0 and page_num > 1:
            break

    return biens


def _parse_html(html: str, dept: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".card.property-card") or soup.select("[class*='property-card']")
    results = []
    for card in cards:
        try:
            bien = _parse_card(card, dept)
            if bien:
                results.append(bien)
        except Exception:
            continue
    return results


def _parse_card(card, dept: str) -> dict | None:
    # L'URL est dans data-href, pas dans href (href="#" pour tous)
    data_href = card.get("data-href", "")
    if not data_href:
        # Fallback : cherche un sous-lien avec vrai href
        a = next((el for el in card.select("a[href]") if el.get("href","") not in ("#", "")), None)
        data_href = a.get("href", "") if a else ""
    if not data_href:
        return None
    url = data_href if data_href.startswith("http") else BASE_URL + data_href

    # ID depuis data-id (fiable) ou URL
    ad_id = card.get("data-id", "")
    if not ad_id:
        id_m = re.search(r"/(\d{4,})/?$", data_href)
        ad_id = id_m.group(1) if id_m else data_href[-12:]

    # Prix, ville, CP : data-* du card (structured data)
    prix_str = card.get("data-price", "")
    prix = float(prix_str) if prix_str else None

    ville = card.get("data-city", "")
    cp = card.get("data-postalcode", "")
    titre_raw = card.get("data-name", "")

    # Surface, pièces, chambres : dans le texte de la carte
    text = card.get_text(" ", strip=True)
    surface = _re_float(r"(\d+(?:[.,]\d+)?)\s*m²", text)
    terrain_m = re.search(r"[Tt]errain\s+([\d\s]+)\s*m²", text.replace(" ", " "))
    terrain = float(terrain_m.group(1).replace(" ", "")) if terrain_m else None
    pieces = _re_int(r"(\d+)\s*pièces?", text)
    chambres = _re_int(r"(\d+)\s*ch(?:ambres?)?", text)
    dpe = _re_str(r"\bDPE\s*:?\s*([A-G])\b", text)

    titre = titre_raw[:150] if titre_raw else f"Maison {pieces or ''} pièces {ville}".strip()

    # Photos : img src/srcset dans la carte
    photos = []
    for img in card.select("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and "http" in src and any(e in src.lower() for e in [".jpg", ".jpeg", ".webp", ".png"]):
            photos.append(src)
        # Aussi cherche dans srcset
        for part in (img.get("srcset", "")).split(","):
            s = part.strip().split(" ")[0]
            if s.startswith("http") and any(e in s.lower() for e in [".jpg", ".jpeg", ".webp", ".png"]):
                photos.append(s)
    photos = list(dict.fromkeys(photos))[:10]

    desc_el = card.select_one("[class*='desc'], [class*='text'], .card-body")
    description = desc_el.get_text(strip=True)[:1200] if desc_el else ""

    return {
        "source": "optimhome",
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
        "agence": "Optimhome",
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
    print(f"\nTotal Optimhome: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

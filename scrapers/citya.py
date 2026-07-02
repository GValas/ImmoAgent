"""
scrapers/citya.py — Citya Immobilier
Méthode : Playwright + parsing HTML
URL : /annonces/vente/maison/{slug-dept}-{code}/ (ex: sarthe-72)
Cards : div.property-card[data-itemid, data-price, data-itemname]
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from scrapers._base import parse_int as _re_int

BASE_URL = "https://www.citya.com"

DEPT_SLUGS = {
    "72": "sarthe-72", "28": "eure-et-loir-28", "45": "loiret-45",
    "89": "yonne-89", "49": "maine-et-loire-49", "37": "indre-et-loire-37",
    "36": "indre-36", "18": "cher-18", "58": "nievre-58",
    "69": "rhone-69", "33": "gironde-33", "34": "herault-34",
    "44": "loire-atlantique-44", "31": "haute-garonne-31",
    "67": "bas-rhin-67", "76": "seine-maritime-76", "59": "nord-59",
    "38": "isere-38", "06": "alpes-maritimes-06", "83": "var-83", "13": "bouches-du-rhone-13",
    "75": "paris-75", "92": "hauts-de-seine-92", "93": "seine-saint-denis-93", "94": "val-de-marne-94",
    "84": "vaucluse-84", "26": "drome-26", "30": "gard-30", "11": "aude-11",
    "63": "puy-de-dome-63", "03": "allier-03", "23": "creuse-23",
    "41": "loir-et-cher-41", "61": "orne-61", "53": "mayenne-53",
    "86": "vienne-86", "79": "deux-sevres-79", "85": "vendee-85", "87": "haute-vienne-87",
    "01": "ain-01", "02": "aisne-02", "07": "ardeche-07", "08": "ardennes-08",
    "09": "ariege-09", "10": "aube-10", "12": "aveyron-12", "14": "calvados-14",
    "15": "cantal-15", "16": "charente-16", "17": "charente-maritime-17",
    "19": "correze-19", "21": "cote-dor-21", "22": "cotes-darmor-22",
    "24": "dordogne-24", "25": "doubs-25", "27": "eure-27", "29": "finistere-29",
    "32": "gers-32", "35": "ille-et-vilaine-35", "39": "jura-39",
    "40": "landes-40", "42": "loire-42", "43": "haute-loire-43",
    "47": "lot-et-garonne-47", "48": "lozere-48", "50": "manche-50",
    "51": "marne-51", "52": "haute-marne-52", "54": "meurthe-et-moselle-54",
    "55": "meuse-55", "56": "morbihan-56", "57": "moselle-57",
    "60": "oise-60", "62": "pas-de-calais-62", "64": "pyrenees-atlantiques-64",
    "65": "hautes-pyrenees-65", "66": "pyrenees-orientales-66",
    "68": "haut-rhin-68", "70": "haute-saone-70", "71": "saone-et-loire-71",
    "73": "savoie-73", "74": "haute-savoie-74", "77": "seine-et-marne-77",
    "78": "yvelines-78", "80": "somme-80", "81": "tarn-81", "82": "tarn-et-garonne-82",
    "88": "vosges-88", "90": "territoire-de-belfort-90",
    "91": "essonne-91", "95": "val-doise-95",
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
                print(f"[Citya] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Citya] Erreur dept {dept}: {e}")
        await browser.close()

    return results


async def _scrape_dept(context, dept: str, prix_min: int, prix_max: int, surface_min: int) -> list[dict]:
    slug = DEPT_SLUGS.get(dept)
    if not slug:
        return []

    biens = []
    seen_ids = set()

    for page_num in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/annonces/vente/maison/{slug}/"
        if page_num > 1:
            url += f"?page={page_num}"

        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector("div.property-card", timeout=10000)
            except Exception:
                pass
            await asyncio.sleep(4)
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
    cards = soup.select("div.property-card")
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
    ad_id = card.get("data-itemid", "")
    if not ad_id:
        return None

    # URL depuis le lien interne de la card
    link_el = card.select_one("a[href*='/annonces/vente/maison/']")
    if link_el:
        url = link_el.get("href", "")
        if url.startswith("/"):
            url = BASE_URL + url
    else:
        url = f"{BASE_URL}/annonces/vente/maison/{ad_id}"

    # Prix depuis data-price (entier) ou texte
    prix_raw = card.get("data-price", "")
    try:
        prix = float(prix_raw) if prix_raw else None
    except ValueError:
        txt_price = card.select_one("strong")
        prix = _re_float(r"([\d\s\xa0]+)\s*€", txt_price.get_text() if txt_price else "") if txt_price else None

    # Pièces + surface depuis data-itemname (ex: "Maison 4 pièces 96.43m²")
    item_name = card.get("data-itemname", "")
    pieces = _re_int(r"(\d+)\s*pièces?", item_name)
    surface = _re_float(r"(\d+(?:[.,]\d+)?)\s*m²", item_name)
    type_bien = card.get("data-category", "maison").lower()

    # Ville + CP depuis le paragraphe de localisation (ex: "Le Mans (72000)")
    loc_el = card.select_one("p.text-neutral-600")
    loc_text = loc_el.get_text(strip=True) if loc_el else card.get_text(" ", strip=True)
    city_m = re.search(r"([A-Za-zÀ-ÿ\s\-]+)\s*\((\d{5})\)", loc_text)
    ville = city_m.group(1).strip() if city_m else ""
    cp = city_m.group(2) if city_m else ""

    # Vérification département via CP
    if cp and not cp.startswith(dept.zfill(2)):
        return None

    # Titre
    titre = f"{type_bien.capitalize()} {pieces or ''}p. {ville}".strip()[:150]

    # Photos : img dans la card avec src relatif Citya
    photos = []
    for img in card.select("img[src]"):
        src = img.get("src", "")
        if src and "/media/images/" in src:
            if src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = list(dict.fromkeys(photos))[:10]

    return {
        "source": "citya",
        "url": url,
        "id_annonce": str(ad_id),
        "titre": titre,
        "type_bien": type_bien,
        "description": item_name,
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Citya Immobilier",
    }


def _re_float(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text)
    if m:
        try:
            return float(m.group(1).replace("\xa0", "").replace(" ", "").replace(",", "."))
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
    print(f"\nTotal Citya: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

"""
scrapers/megagence.py — megAgence (réseau mandataires)
Méthode : Playwright + parsing HTML
URL : /acheter/maison/{slug-dept}/ — slug = nom département sans numéro (ex: "sarthe")
Cards : li[class*='list-prop-li'] > a.list-prop-li-container[href]
Interface : async def search(criteres: dict) -> list[dict]
"""
import re
import asyncio

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


BASE_URL = "https://www.megagence.com"

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
    "01": "ain", "02": "aisne", "07": "ardeche", "08": "ardennes",
    "09": "ariege", "10": "aube", "12": "aveyron", "14": "calvados",
    "15": "cantal", "16": "charente", "17": "charente-maritime",
    "19": "correze", "21": "cote-dor", "22": "cotes-darmor",
    "24": "dordogne", "25": "doubs", "27": "eure", "29": "finistere",
    "32": "gers", "35": "ille-et-vilaine", "39": "jura",
    "40": "landes", "42": "loire", "43": "haute-loire",
    "47": "lot-et-garonne", "48": "lozere", "50": "manche",
    "51": "marne", "52": "haute-marne", "54": "meurthe-et-moselle",
    "55": "meuse", "56": "morbihan", "57": "moselle",
    "60": "oise", "62": "pas-de-calais", "64": "pyrenees-atlantiques",
    "65": "hautes-pyrenees", "66": "pyrenees-orientales",
    "68": "haut-rhin", "70": "haute-saone", "71": "saone-et-loire",
    "73": "savoie", "74": "haute-savoie", "77": "seine-et-marne",
    "78": "yvelines", "80": "somme", "81": "tarn", "82": "tarn-et-garonne",
    "88": "vosges", "90": "territoire-de-belfort",
    "91": "essonne", "95": "val-doise",
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
                print(f"[Megagence] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Megagence] Erreur dept {dept}: {e}")
        await browser.close()

    return results


async def _scrape_dept(context, dept: str, prix_min: int, prix_max: int, surface_min: int) -> list[dict]:
    slug = DEPT_SLUGS.get(dept)
    if not slug:
        return []

    biens = []
    seen_ids = set()

    for page_num in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/acheter/maison/{slug}/"
        if page_num > 1:
            url += f"?page={page_num}"

        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector("li[class*='list-prop-li']", timeout=10000)
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
    cards = soup.select("li[class*='list-prop-li']")
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
    # Lien principal
    link_el = card.select_one("a.list-prop-li-container[href]")
    if not link_el:
        link_el = card.select_one("a[href]")
    if not link_el:
        return None
    url = link_el.get("href", "")
    if not url:
        return None
    if url.startswith("/"):
        url = BASE_URL + url

    # Extraire la référence depuis l'URL (ex: /annonces/achat/maison/la-milesse-72650/204244)
    ref_m = re.search(r"/(\d{5,})(?:\?|$)", url)
    ad_id = ref_m.group(1) if ref_m else ""
    if not ad_id:
        # Essayer depuis le texte "Réf. 204244"
        txt = card.get_text(" ", strip=True)
        ref_m2 = re.search(r"[Rr]é[fs]\.?\s*(\d{4,})", txt)
        ad_id = ref_m2.group(1) if ref_m2 else url.split("/")[-1].split("?")[0]

    # Texte de la description (figcaption)
    desc_el = card.select_one("figcaption, .list-prop-desc")
    desc_text = desc_el.get_text(" ", strip=True) if desc_el else card.get_text(" ", strip=True)

    # Prix
    prix_el = card.select_one(".list-prop-desc-price, [class*='price'], [class*='prix']")
    prix_text = prix_el.get_text(" ", strip=True) if prix_el else desc_text
    prix = _re_float(r"([\d\s\xa0]+)\s*€", prix_text.replace("\xa0", " ").replace(" ", " "))

    # Surface (ex: "93m²" ou "93 m²")
    surface = _re_float(r"(\d+(?:[.,]\d+)?)\s*m²", desc_text)

    # Pièces (ex: "5 pièces")
    pieces = _re_int(r"(\d+)\s*pièces?", desc_text)

    # Code postal + ville (ex: "72650 - La Milesse")
    loc_m = re.search(r"(\d{5})\s*[-–]\s*([^\n\d]{3,40}?)(?:\s*Offre|$|\n)", desc_text)
    cp = loc_m.group(1) if loc_m else ""
    ville = loc_m.group(2).strip() if loc_m else ""

    # Si pas trouvé, essayer depuis l'URL (ex: /la-milesse-72650/)
    if not cp:
        url_cp_m = re.search(r"-(\d{5})/", url)
        cp = url_cp_m.group(1) if url_cp_m else ""
    if not ville:
        url_ville_m = re.search(r"/maison/([a-z-]+)-\d{5}/", url)
        if url_ville_m:
            ville = url_ville_m.group(1).replace("-", " ").title()

    # Vérification département depuis CP
    if cp and not cp.startswith(dept.zfill(2)):
        return None

    # Type de bien (premier mot du texte)
    type_m = re.match(r"^\s*(Maison|Villa|Pavillon|Propriété|Corps de ferme|Longère)", desc_text, re.IGNORECASE)
    type_bien = type_m.group(1).lower() if type_m else "maison"

    # Titre
    titre = f"{type_bien.capitalize()} {pieces or ''}p. {ville}".strip()[:150]

    # Terrain dans la description
    terrain_m = re.search(r"(?:terrain|parcelle)[^\d]{0,20}(\d[\d\s]*)\s*m²", desc_text, re.IGNORECASE)
    terrain = float(terrain_m.group(1).replace(" ", "")) if terrain_m else None

    # DPE
    dpe = _re_str(r"\bDPE\s*:?\s*(?:classe\s*)?([A-G])\b", desc_text)

    # Photos
    photos = []
    for el in card.select("img, source"):
        src = el.get("src", "") or el.get("data-src", "")
        if src and any(e in src.lower() for e in [".jpg", ".jpeg", ".webp", ".png"]):
            if src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = list(dict.fromkeys(photos))[:10]

    return {
        "source": "megagence",
        "url": url,
        "id_annonce": str(ad_id),
        "titre": titre,
        "type_bien": type_bien,
        "description": desc_text[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": terrain,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "megAgence",
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
    print(f"\nTotal megAgence: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

"""
scrapers/lesiteimmo.py — LeSiteImmo
Méthode : httpx + JSON-LD server-side rendered (pas de Playwright)
URL : /acheter/maison/{slug-dept}?page=N (ex: sarthe-72)
Données : CollectionPage.mainEntity.itemListElement — 25 items/page
  - offers.price (EUR)
  - address.addressLocality + postalCode
  - url : /acheter/maison-Xpieces/{ville}-{cp}/{id}
  - image : liste URLs photos
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import json
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.lesiteimmo.com"

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
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}


async def search(criteres: dict) -> list[dict]:
    departements = criteres.get("departements", [])
    prix_max = criteres.get("prix_max", 600000)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 80)

    results = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        for dept in departements:
            try:
                biens = await _scrape_dept(client, str(dept), prix_min, prix_max, surface_min)
                results.extend(biens)
                print(f"[LeSiteImmo] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[LeSiteImmo] Erreur dept {dept}: {e}")

    return results


async def _scrape_dept(client: httpx.AsyncClient, dept: str, prix_min: int, prix_max: int, surface_min: int) -> list[dict]:
    slug = DEPT_SLUGS.get(dept)
    if not slug:
        return []

    biens = []
    seen_ids = set()

    for page_num in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/acheter/maison/{slug}"
        if page_num > 1:
            url += f"?page={page_num}"

        try:
            r = await client.get(url)
            if r.status_code != 200:
                break
        except Exception:
            break

        items = _parse_jsonld(r.text)
        if not items:
            break

        new_found = 0
        for item in items:
            bien = _parse_item(item, dept)
            if not bien:
                continue
            if bien["id_annonce"] in seen_ids:
                continue
            if bien.get("prix") and prix_max and bien["prix"] > prix_max:
                continue
            if prix_min and bien.get("prix") and bien["prix"] < prix_min:
                continue
            if bien.get("surface") and surface_min and bien["surface"] < surface_min:
                continue
            seen_ids.add(bien["id_annonce"])
            biens.append(bien)
            new_found += 1

        if new_found == 0 and page_num > 1:
            break

    return biens


def _parse_jsonld(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if data.get("@type") == "CollectionPage":
                return data.get("mainEntity", {}).get("itemListElement", [])
        except Exception:
            continue
    return []


def _parse_item(list_item: dict, dept: str) -> dict | None:
    item = list_item.get("item", {})
    if not item:
        return None

    url = item.get("url", "")
    if not url:
        return None

    # ID depuis la fin de l'URL (/.../{id})
    id_m = re.search(r"/(\d{6,})$", url)
    ad_id = id_m.group(1) if id_m else url.split("/")[-1]
    if not ad_id:
        return None

    # Pièces depuis l'URL (/maison-Xpieces/)
    pieces_m = re.search(r"maison-(\d+)pieces", url)
    pieces = int(pieces_m.group(1)) if pieces_m else None

    # Prix depuis offers.price
    offers = item.get("offers", {})
    prix = offers.get("price")
    if prix is not None:
        try:
            prix = float(prix)
        except (ValueError, TypeError):
            prix = None

    # Adresse
    addr = item.get("address", {})
    ville = addr.get("addressLocality", "")
    cp = addr.get("postalCode", "")

    # Vérification département
    if cp and not cp.startswith(dept.zfill(2)):
        return None

    # Surface depuis la description (ex: "maison de 115 m²", "surface habitable de 220 m²")
    desc = item.get("description", "") or ""
    surface = _parse_surface(desc)

    # Titre
    titre = (item.get("name", "") or "").strip()[:150]
    if not titre:
        titre = f"Maison {pieces or ''}p. {ville}"

    # Photos
    images = item.get("image", [])
    if isinstance(images, str):
        images = [images]
    photos = [img for img in images if img and img.startswith("http")][:10]

    # DPE depuis description
    dpe_m = re.search(r"\bDPE\s*:?\s*(?:classe\s*)?([A-G])\b", desc, re.IGNORECASE)
    dpe = dpe_m.group(1).upper() if dpe_m else None

    # Terrain depuis description
    terrain_m = re.search(r"terrain\s+(?:de\s+)?(\d[\d\s]*)\s*m²", desc, re.IGNORECASE)
    terrain = float(terrain_m.group(1).replace(" ", "")) if terrain_m else None

    return {
        "source": "lesiteimmo",
        "url": url,
        "id_annonce": str(ad_id),
        "titre": titre,
        "type_bien": "maison",
        "description": desc[:1200],
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
        "agence": None,
    }


def _parse_surface(desc: str) -> float | None:
    if not desc:
        return None
    # Surface habitable prioritaire (exclure terrain)
    for pat in [
        r"surface\s+habitable\s+(?:de\s+)?(\d+(?:[.,]\d+)?)\s*m²",
        r"(\d+(?:[.,]\d+)?)\s*m²\s+(?:habitables?|de\s+surface\s+habitable)",
        r"maison\s+(?:de\s+)?(\d+)\s*m²",
        r"(?:environ|soit)\s+(\d+)\s*m²",
    ]:
        m = re.search(pat, desc, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except Exception:
                pass
    # Fallback : premier m² mentionné qui ne suit pas "terrain"
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*m²", desc, re.IGNORECASE):
        start = max(0, m.start() - 30)
        context = desc[start:m.start()].lower()
        if "terrain" not in context and "parcelle" not in context and "jardin" not in context:
            try:
                val = float(m.group(1).replace(",", "."))
                if 30 <= val <= 1000:
                    return val
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
    print(f"\nTotal LeSiteImmo: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

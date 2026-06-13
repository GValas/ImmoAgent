"""
scrapers/arthurimmo.py — Arthur Immo (arthurimmo.com)
Méthode : scrape_simple (httpx) — Laravel + Livewire SSR, données dans div[wire:id]
Interface : async def search(criteres: dict) -> list[dict]
Note : ~13-30 annonces/dept, pagination ?page=N — skip 49 (0 annonces)
"""
import asyncio
import json
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.arthurimmo.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Department code → URL slug on arthurimmo.com/immobilier/achat/{slug}/bien-maison/
# 49 skipped — 0 Arthur Immo agencies in Maine-et-Loire
DEPT_SLUGS = {
    "72": "sarthe-72",
    "53": "mayenne-53",
    "37": "indre-et-loire-37",
    "45": "loiret-45",
    "41": "loir-et-cher-41",
    "18": "cher-18",
    "28": "eure-et-loir-28",
    "36": "indre-36",
    "89": "yonne-89",
    "58": "nievre-58",
}


async def search(criteres: dict) -> list[dict]:
    departements = criteres.get("departements", [])
    prix_max = criteres.get("prix_max", 600000)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 80)

    results = []
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
        for dept in departements:
            dept_str = str(dept).zfill(2)
            slug = DEPT_SLUGS.get(dept_str)
            if not slug:
                continue
            try:
                biens = await _scrape_dept(client, dept_str, slug, prix_max, prix_min, surface_min)
                results.extend(biens)
                print(f"[ArthurImmo] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[ArthurImmo] Erreur dept {dept}: {e}")
            await asyncio.sleep(1)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    slug: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens = []
    for page in range(1, 6):
        url = f"{BASE_URL}/immobilier/achat/{slug}/bien-maison/?page={page}"
        r = await client.get(url, timeout=15)
        r.raise_for_status()
        page_biens = _parse_html(r.text, dept, prix_max, prix_min, surface_min)
        biens.extend(page_biens)
        if len(page_biens) < 12:
            break
        await asyncio.sleep(0.5)
    return biens


def _parse_html(
    html: str, dept: str, prix_max: int, prix_min: int, surface_min: int
) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for div in soup.find_all("div", attrs={"wire:id": True}):
        wire_data_str = div.get("wire:initial-data", "")
        if not wire_data_str:
            continue
        try:
            wire_data = json.loads(wire_data_str)
        except Exception:
            continue
        if wire_data.get("fingerprint", {}).get("name") != "property.card":
            continue
        try:
            bien = _parse_card(div, dept)
            if not bien:
                continue
            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p > prix_max:
                continue
            if prix_min and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue
            results.append(bien)
        except Exception:
            continue
    return results


def _parse_card(div, dept: str) -> dict | None:
    text = div.get_text(" ", strip=True)

    # Link + ID
    link_el = div.select_one("a[href*='/annonces/achat/maison/']")
    if not link_el:
        return None
    href = link_el.get("href", "")
    url = href if href.startswith("http") else BASE_URL + href

    id_m = re.search(r"/(\d+)\.htm", url)
    ad_id = id_m.group(1) if id_m else url[-15:]

    # Price
    price_m = re.search(r"([\d][\d\s\xa0  ]+)\s*€", text)
    if not price_m:
        return None
    try:
        prix = float(
            price_m.group(1)
            .replace("\xa0", "")
            .replace(" ", "")
            .replace(" ", "")
            .replace(" ", "")
        )
    except Exception:
        return None
    if not prix:
        return None

    # Surface
    surf_m = re.search(r"([\d,\.]+)\s*m²", text)
    surface = float(surf_m.group(1).replace(",", ".")) if surf_m else None

    # Rooms
    pieces_m = re.search(r"(\d+)\s*pièces?", text, re.IGNORECASE)
    pieces = int(pieces_m.group(1)) if pieces_m else None

    chambres_m = re.search(r"(\d+)\s*chambres?", text, re.IGNORECASE)
    chambres = int(chambres_m.group(1)) if chambres_m else None

    # City and CP from URL slug: /annonces/achat/maison/{city-slug}-{5digits}/
    url_m = re.search(r"/annonces/achat/maison/([^/]+)-(\d{5})/", url)
    cp = ""
    ville = ""
    if url_m:
        city_slug = url_m.group(1)
        cp = url_m.group(2)
        ville = city_slug.replace("-", " ").title()

    # Photos (CDN: media.studio-net.fr)
    photos = []
    for img in div.select("img"):
        src = img.get("src", "") or img.get("data-src", "")
        if src and "studio-net.fr" in src:
            photos.append(src)
    photos = photos[:10]

    titre = f"Maison — {ville} ({cp})"

    return {
        "source": "arthurimmo",
        "url": url,
        "id_annonce": ad_id,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Arthur Immo",
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria
    criteres = load_criteria()
    biens = asyncio.run(search({
        "departements": criteres.departements[:5],
        "prix_max": criteres.prix_max,
        "prix_min": getattr(criteres, "prix_min", 0),
        "surface_min": criteres.surface_min,
    }))
    print(f"\nTotal Arthur Immo: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

"""
scrapers/foncia.py — Foncia Transaction (réseau national)
Méthode : httpx pur — Angular SSR, filtre dept fonctionnel
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import parse_int as _re_int
from scrapers._base import parse_str_upper as _re_str

BASE_URL = "https://www.foncia.com"

DEPT_SLUGS = {
    "72": "sarthe-72", "28": "eure-et-loir-28", "45": "loiret-45",
    "89": "yonne-89", "49": "maine-et-loire-49", "37": "indre-et-loire-37",
    "36": "indre-36", "18": "cher-18", "58": "nievre-58",
    "41": "loir-et-cher-41", "53": "mayenne-53",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

MAX_PAGES = 6


async def search(criteres: dict) -> list[dict]:
    departements = criteres.get("departements", [])
    prix_max = criteres.get("prix_max", 600000)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 80)
    pieces_min = criteres.get("pieces_min", 0)

    results = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        for dept in departements:
            dept_str = str(dept).zfill(2)
            slug = DEPT_SLUGS.get(dept_str)
            if not slug:
                continue
            try:
                biens = await _fetch_dept(client, dept_str, slug, prix_max, prix_min, surface_min, pieces_min)
                results.extend(biens)
                print(f"[Foncia] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Foncia] Erreur dept {dept}: {e}")

    return results


async def _fetch_dept(client, dept: str, slug: str, prix_max: int, prix_min: int,
                      surface_min: int, pieces_min: int) -> list[dict]:
    results = []
    for page in range(1, MAX_PAGES + 1):
        url = (
            f"{BASE_URL}/achat/{slug}/maison/"
            f"?surfaceMin={surface_min}"
            f"&prixMax={prix_max}"
            + (f"&prixMin={prix_min}" if prix_min else "")
            + (f"&nbPiecesMin={pieces_min}" if pieces_min else "")
            + (f"&page={page}" if page > 1 else "")
        )
        r = await client.get(url)
        if r.status_code != 200:
            break
        biens = _parse_html(r.text, dept)
        if not biens:
            break
        results.extend(biens)
        if len(biens) < 12:
            break
    return results


def _parse_html(html: str, dept: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for card in soup.select("div.foncia-card"):
        try:
            bien = _parse_card(card, dept)
            if bien:
                results.append(bien)
        except Exception:
            continue
    return results


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a.gallery-container, a.foncia-card-title-small, a.foncia-card-title-big")
    if not link:
        return None
    href = link.get("href", "")
    url = href if href.startswith("http") else BASE_URL + href
    if not href:
        return None

    prix_el = card.select_one("h2.foncia-card-price span")
    prix = _re_float(r"([\d\s ]+)\s*€", prix_el.get_text() if prix_el else "")

    surf_el = card.select_one("span.foncia-card-surface")
    surface = _re_float(r"([\d,\.]+)", surf_el.get_text() if surf_el else "")
    if surface:
        surface = float(str(surface).replace(",", "."))

    title_el = card.select_one("span.foncia-card-title-small-title, a.foncia-card-title-big")
    titre = title_el.get_text(strip=True) if title_el else ""

    city_el = card.select_one("p.foncia-card-place")
    city_text = city_el.get_text(strip=True) if city_el else ""
    cp_m = re.search(r"\((\d{5})\)", city_text)
    cp = cp_m.group(1) if cp_m else ""
    ville = re.sub(r"\s*\(\d{5}\)\s*", "", city_text).strip()

    rooms_el = card.select_one("p.foncia-card-bedrooms, [class*=bedroom], [class*=pieces]")
    chambres = _re_int(r"(\d+)\s*ch", rooms_el.get_text() if rooms_el else "")
    pieces = _re_int(r"(\d+)\s*pièces?", titre)

    img = card.select_one("img[src]")
    photos = [img["src"]] if img and img.get("src", "").startswith("http") else []

    dpe_el = card.select_one("span.foncia-card-dpe, [class*=dpe]")
    dpe = _re_str(r"\b([A-G])\b", dpe_el.get_text() if dpe_el else "")

    ref_m = re.search(r"/(\d{8})\.htm", url)
    ad_id = ref_m.group(1) if ref_m else url[-20:]

    desc_el = card.select_one("p.foncia-card-description")
    description = desc_el.get_text(strip=True)[:1200] if desc_el else ""

    if not prix and not surface:
        return None

    return {
        "source": "foncia",
        "url": url,
        "id_annonce": ad_id,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": description,
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Foncia",
    }


def _re_float(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text)
    if m:
        try:
            return float(m.group(1).replace(" ", "").replace("\xa0", "").replace(" ", "").replace(",", "."))
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
        "prix_min": getattr(criteres, "prix_min", 0),
        "surface_min": criteres.surface_min,
    }))
    print(f"\nTotal Foncia: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

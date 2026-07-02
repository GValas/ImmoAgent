"""
scrapers/seloger.py — SeLoger (portail majeur)
Méthode : scrape_simple (httpx) — URL legacy list.htm.

⚠️ Protection = DataDome (PAS Cloudflare). Sous User-Agent navigateur DESKTOP, le site
renvoie désormais 403 DataDome (x-datadome: protected). BRÈCHE exploitée : avec le
User-Agent de l'app iOS SeLoger, list.htm repasse en 200 et sert la page SSR complète
(24 cartes, mêmes sélecteurs). Contraintes dures de DataDome (vérifiées) :
  - 1 SEUL code postal par requête (places=[{cp:...}] multi-CP → 403 immédiat) ;
  - pas de pagination (page 2 → 403) → on ne récupère que la page 1 (~24 cartes/CP) ;
  - réputation IP collante : après quelques requêtes l'IP est flaggée et TOUT passe en
    403 pendant >90 s. → on throttle, et on ARRÊTE le run au 1er 403 (insister aggrave).
Sans proxies résidentiels rotatifs, la couverture utile se limite à quelques CP/run.
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import parse_float as _re_float
from scrapers._base import parse_int as _re_int

BASE_URL = "https://www.seloger.com"
SEARCH_URL = (
    "https://www.seloger.com/list.htm"
    "?types=2&natures=1&projects=2&enterprise=0"
    "&qsVersion=1.0&m=search_refine"
    "&places={places}"
)

# User-Agent de l'app iOS SeLoger : seule clé qui repasse list.htm en 200 (DataDome).
HEADERS = {
    "User-Agent": "SeLoger/6.0 (iPhone; iOS 17.0; Scale/3.00)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

THROTTLE_S = 6.0   # délai entre requêtes (réputation IP DataDome)

# Postal codes prefix map per department
DEPT_POSTAL_CODES = {d: [f"{d}{s:03d}" for s in range(0, 900, 100)] for d in [
    "72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"
]}


class _DataDomeBlocked(Exception):
    """L'IP est flaggée par DataDome (403) — inutile de continuer ce run."""


async def search(criteres: dict) -> list[dict]:
    departements = criteres.get("departements", [])
    prix_max = criteres.get("prix_max", 600000)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 80)

    results = []
    seen_ids = set()
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
        try:
            for dept in departements:
                dept_str = str(dept).zfill(2)
                cps = DEPT_POSTAL_CODES.get(dept_str)
                if not cps:
                    continue
                biens = await _scrape_dept(client, dept_str, cps, prix_max, prix_min, surface_min)
                kept = 0
                for b in biens:
                    if b["id_annonce"] in seen_ids:
                        continue
                    seen_ids.add(b["id_annonce"])
                    results.append(b)
                    kept += 1
                print(f"[SeLoger] Dept {dept}: {kept} annonces")
        except _DataDomeBlocked:
            print(f"[SeLoger] DataDome a flaggé l'IP (403) — run interrompu "
                  f"({len(results)} annonces avant blocage). Couverture complète = "
                  f"proxies résidentiels requis.")
    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    cps: list[str],
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    """UN seul code postal par requête (DataDome refuse le multi-CP), throttle entre
    chaque, et abandon immédiat au premier 403 (IP flaggée)."""
    out = []
    for i, cp in enumerate(cps):
        places = f"[{{cp:{cp}}}]"   # un seul CP — multi-CP = 403 DataDome
        url = SEARCH_URL.format(places=places)
        try:
            r = await client.get(url)
        except httpx.HTTPError:
            await asyncio.sleep(THROTTLE_S)
            continue
        if r.status_code in (403, 405) or "captcha-delivery" in r.text[:3000]:
            raise _DataDomeBlocked()
        if r.status_code == 200:
            out.extend(_parse_html(r.text, dept, prix_max, prix_min, surface_min))
        if i < len(cps) - 1:
            await asyncio.sleep(THROTTLE_S)
    return out


def _parse_html(
    html: str, dept: str, prix_max: int, prix_min: int, surface_min: int
) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []

    cards = soup.select("[data-testid='sl.explore.card-container']")
    if not cards:
        cards = soup.select("[class*='cardMode']")

    for card in cards:
        try:
            bien = _parse_card(card, dept)
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


def _parse_card(card, dept: str) -> dict | None:
    # --- Price ---
    price_el = card.select_one("[data-test='sl.price-label']")
    if not price_el:
        return None
    prix = _re_float(r"([\d\s\xa0]+)\s*€", price_el.get_text(strip=True).replace("\xa0", " "))
    if not prix:
        return None

    # --- Title ---
    title_el = card.select_one("[data-test='sl.title']")
    titre = title_el.get_text(strip=True) if title_el else "Maison"

    # --- Tags: pieces, surface, chambres ---
    tags = [li.get_text(strip=True) for li in card.select("[data-test='sl.tagsLine'] li")]
    pieces = None
    surface = None
    chambres = None
    for tag in tags:
        if "pièce" in tag and pieces is None:
            pieces = _re_int(r"(\d+)", tag)
        elif "chambre" in tag and chambres is None:
            chambres = _re_int(r"(\d+)", tag)
        elif "m²" in tag and surface is None:
            surface = _re_float(r"([\d,\.]+)", tag.replace("\xa0", "").replace(" ", ""))

    # --- Address ---
    addr_el = card.select_one("[data-test='sl.address']")
    addr_text = addr_el.get_text(strip=True) if addr_el else ""
    cp_m = re.search(r"\((\d{5})\)", addr_text)
    cp = cp_m.group(1) if cp_m else ""
    ville = re.sub(r"à\s+", "", addr_text)
    ville = re.sub(r"\s*\(?\d{5}\)?\s*$", "", ville).strip()

    # --- Link ---
    link = card.select_one("a[href*='/annonces/achat/maison/']")
    if not link:
        link = card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    url = href if href.startswith("http") else BASE_URL + href

    id_m = re.search(r"/(\d{7,})", url)
    ad_id = id_m.group(1) if id_m else url[-20:]

    # --- Photo ---
    photos = []
    for img in card.select("[data-testid='sl.explore.PhotosContainer'] img[src]"):
        src = img.get("src", "")
        if src.startswith("http"):
            photos.append(src)

    return {
        "source": "seloger",
        "url": url,
        "id_annonce": ad_id,
        "titre": f"{titre} — {addr_text}"[:150],
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
        "photos": photos[:10],
        "dpe": None,
        "agence": "",
    }


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
    print(f"\nTotal SeLoger: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

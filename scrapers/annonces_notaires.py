"""
scrapers/annonces_notaires.py — Immonot (annonces des notaires de France)
Méthode : POST avec CSRF token sur /immobilier.do
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.immonot.com"
HOME_URL = f"{BASE_URL}/"
SEARCH_URL = f"{BASE_URL}/immobilier.do"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}


async def search(criteres: dict) -> list[dict]:
    departements = criteres.get("departements", [])
    prix_max = criteres.get("prix_max", 600000)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 80)
    pieces_min = criteres.get("pieces_min", 4)

    results = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        # Récupérer le CSRF une seule fois
        csrf = await _get_csrf(client)
        if not csrf:
            print("[Immonot] CSRF introuvable — abandon")
            return []

        for dept in departements:
            try:
                biens = await _fetch_dept(client, csrf, str(dept), prix_min, prix_max, surface_min)
                results.extend(biens)
            except Exception as e:
                print(f"[Immonot] Erreur dept {dept}: {e}")

    return results


async def _get_csrf(client: httpx.AsyncClient) -> str:
    r = await client.get(HOME_URL)
    soup = BeautifulSoup(r.text, "html.parser")
    inp = soup.select_one('input[name="WAEFIK_CSRF_TOKEN"]')
    return inp.get("value", "") if inp else ""


async def _fetch_dept(client, csrf: str, dept: str,
                      prix_min: int, prix_max: int, surface_min: int) -> list[dict]:
    prix_range = f"{prix_min if prix_min else 0}-{prix_max}"
    data = {
        "indexDebut": "0",
        "action": "recherche",
        "WAEFIK_CSRF_TOKEN": csrf,
        "typesBiens": "MAIS,PROP",
        "transactions": "VENT",
        "localite": dept,
        "rayon": "0",
        "reference": "",
        "surfaceInt": f"{surface_min}-0",
        "surfaceExt": "0-0",
        "prix": prix_range,
    }
    r = await client.post(
        SEARCH_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return _parse_page(r.text, dept)


def _parse_page(html: str, dept: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.il-card")
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
    ad_id = card.get("id", "")

    # URL
    link = card.select_one("a.reset-link, a.js-mirror-link")
    if not link:
        return None
    url = link.get("href", "")
    if url.startswith("/"):
        url = BASE_URL + url

    # Titre + localisation
    title_el = card.select_one(".il-card-title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    locale_el = card.select_one(".il-card-locale")
    locale_text = locale_el.get_text(strip=True) if locale_el else ""
    # Ex: "La Chartre-sur-le-Loir - 72340"
    ville = locale_text.split("-")[0].strip() if locale_text else ""
    cp = re.search(r"\b(\d{5})\b", locale_text)
    code_postal = cp.group(1) if cp else ""

    # Prix
    price_el = card.select_one(".il-card-price strong")
    prix_text = price_el.get_text(" ", strip=True) if price_el else ""
    prix = _parse_number(prix_text)

    # Description
    desc_el = card.select_one(".il-card-excerpt")
    description = desc_el.get_text(" ", strip=True)[:1200] if desc_el else ""

    # Surface intérieure et terrain depuis les quickview items
    surface = None
    terrain = None
    pieces = None
    chambres = None
    for item in card.select(".il-card-quickview-item"):
        label_el = item.select_one("span")
        value_el = item.select_one("strong")
        if not label_el or not value_el:
            continue
        label = label_el.get_text(strip=True).lower()
        value_text = value_el.get_text(" ", strip=True)
        value = _parse_number(value_text)
        if "intérieur" in label or "surface" in label:
            surface = value
        elif "extérieur" in label or "terrain" in label:
            terrain = value
        elif "pièce" in label:
            pieces = int(value) if value else None
        elif "chb" in label or "chambre" in label:
            chambres = int(value) if value else None

    # Photos
    photos = []
    for img in card.select(".il-card-img[data-src], img[data-src]"):
        src = img.get("data-src", "")
        if src and not src.endswith(".gif"):
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)

    # Agence (notaire)
    notary_el = card.select_one(".js-tooltip-target")
    agence = notary_el.get_text(strip=True) if notary_el else ""

    if not url:
        return None

    return {
        "source": "annonces_notaires",
        "url": url,
        "id_annonce": ad_id or url,
        "titre": titre,
        "type_bien": "maison",
        "description": description,
        "departement": dept,
        "ville": ville,
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:10],
        "dpe": _extract_dpe(description),
        "agence": agence,
    }


def _parse_number(text: str) -> float | None:
    text = text.replace("\xa0", " ").replace(" ", " ").replace(" ", " ")
    m = re.search(r"(\d[\d ]*(?:[.,]\d+)?)", text)
    if m:
        try:
            return float(m.group(1).replace(" ", "").replace(",", "."))
        except Exception:
            pass
    return None


def _extract_dpe(text: str) -> str | None:
    m = re.search(r"\bDPE\s*:?\s*([A-G])\b", text, re.IGNORECASE)
    return m.group(1).upper() if m else None


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria
    criteres = load_criteria()
    biens = asyncio.run(search({
        "departements": criteres.departements[:3],
        "prix_max": criteres.prix_max,
        "prix_min": criteres.prix_min,
        "surface_min": criteres.surface_min,
        "pieces_min": criteres.pieces_min,
    }))
    print(f"\nTotal Immonot: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

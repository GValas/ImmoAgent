"""
scrapers/laforet.py — Laforêt Immobilier
Méthode : scrape_simple (httpx) — SSR Symfony, données GTM dans les attributs
Interface : async def search(criteres: dict) -> list[dict]
Note : ~10-120 annonces/dept, pagination ?page=N — skip 72 et 36 (pas de page dédiée)
"""
import re
import asyncio

import httpx
from bs4 import BeautifulSoup


BASE_URL = "https://www.laforet.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Department code → URL slug on laforet.com/departement/achat-maison-{slug}
# 72 and 36 redirect to region pages without dept filtering — skipped
DEPT_SLUGS = {
    "53": "mayenne",
    "49": "maine-et-loire",
    "37": "indre-et-loire",
    "45": "loiret",
    "41": "loir-et-cher",
    "18": "cher",
    "28": "eure-et-loir",
    "89": "yonne",
    "58": "nievre",
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
                print(f"[Laforet] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Laforet] Erreur dept {dept}: {e}")
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
        url = f"{BASE_URL}/departement/achat-maison-{slug}?page={page}"
        r = await client.get(url, timeout=15)
        r.raise_for_status()
        page_biens = _parse_html(r.text, dept, prix_max, prix_min, surface_min)
        biens.extend(page_biens)
        if len(page_biens) < 35:
            break
        await asyncio.sleep(0.5)
    return biens


def _parse_html(
    html: str, dept: str, prix_max: int, prix_min: int, surface_min: int
) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for btn in soup.select("button[data-gtm-item-id-param]"):
        try:
            bien = _parse_card(btn, dept)
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


def _parse_card(btn, dept: str) -> dict | None:
    ad_id = btn.get("data-gtm-item-id-param", "")
    if not ad_id:
        return None

    price_str = btn.get("data-gtm-item-price-param", "0")
    try:
        prix = float(price_str)
    except (ValueError, TypeError):
        return None
    if not prix:
        return None

    type_bien = btn.get("data-gtm-item-type-param", "Maison")
    if type_bien.lower() not in ("maison", "house", "villa", "pavillon"):
        return None

    ville = btn.get("data-gtm-item-city-param", "").title()
    cp = btn.get("data-gtm-item-zipcode-param", "")
    surface_str = btn.get("data-gtm-item-size-param", "")
    try:
        surface = float(surface_str) if surface_str else None
    except ValueError:
        surface = None

    criteria = btn.get("data-gtm-item-criteria-param", "")
    chambres = _re_int(r"(\d+)\s*chambre", criteria, re.IGNORECASE)
    pieces = _re_int(r"(\d+)\s*pi[eè]ce", criteria, re.IGNORECASE)

    # Navigate up to the card div to find link and photos
    card = btn
    for _ in range(10):
        if card.get("class") and "border-border-gray" in " ".join(card.get("class", [])):
            break
        card = card.parent
        if card is None:
            break

    url = ""
    photos = []
    if card:
        link_el = card.select_one("a[href*='/acheter/']")
        if link_el:
            href = link_el.get("href", "")
            url = href if href.startswith("http") else BASE_URL + href

        for img in card.select("img[src*='/glide/']"):
            src = img.get("src", "")
            if src and "hidden" not in " ".join(img.get("class", [])):
                full_src = src if src.startswith("http") else BASE_URL + src
                photos.append(full_src)
        photos = photos[:10]

    if not url:
        url = f"{BASE_URL}/annonce/maison-{ad_id}"

    titre = f"Maison — {ville} ({cp})"

    return {
        "source": "laforet",
        "url": url,
        "id_annonce": ad_id,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": criteria,
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
        "agence": "Laforêt",
    }


def _re_int(pattern: str, text: str, flags: int = 0) -> int | None:
    m = re.search(pattern, text, flags)
    return int(m.group(1)) if m else None


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria
    criteres = load_criteria()
    biens = asyncio.run(search({
        "departements": criteres.departements[:4],
        "prix_max": criteres.prix_max,
        "prix_min": getattr(criteres, "prix_min", 0),
        "surface_min": criteres.surface_min,
    }))
    print(f"\nTotal Laforêt: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

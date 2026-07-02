"""scrapers/greenacres.py — Green-Acres (portail international, propriétés de caractère)

Méthode : scrape_simple (httpx) — SSR React
URL pattern : /immobilier/{dept-slug}?page=N
Cards : div.announce-card:not(.skeleton)
  - URL    : data-o (base64 encodé)
  - Titre  : attribut title
  - Prix   : strong.info-price
  - Loc    : div.announce-localisation  →  "Ville (Dept)"
  - Chars  : div.characteristics  →  "420 m² 5 000 m² de terrain 13 pièces"
  - Photos : [data-thumb-src]

Spécificité : annonces de prestige / caractère — peu d'appartements
              ~24 annonces/page, 3-6 pages/dept
Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import base64
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int

BASE_URL = "https://www.green-acres.fr"
PHOTOS_PER_CARD = 10
MAX_PAGES = 8


# Code département → slug URL green-acres.fr/immobilier/{slug}
DEPT_SLUGS: dict[str, str] = {
    "72": "sarthe",
    "28": "eure-et-loir",
    "45": "loiret",
    "89": "yonne",
    "49": "maine-et-loire",
    "37": "indre-et-loire",
    "36": "indre",
    "18": "cher",
    "58": "nievre",
    "41": "loir-et-cher",
    "53": "mayenne",
    # Couverture étendue si besoin
    "27": "eure",
    "76": "seine-maritime",
    "61": "orne",
    "50": "manche",
}

# Mots-clés dans le titre pour considérer comme maison/propriété
_HOUSE_KEYWORDS = re.compile(
    r"maison|villa|longère|ferme|manoir|château|moulin|propriété|demeure|corps de ferme|gîte|mas",
    re.IGNORECASE,
)
# Mots à exclure explicitement
_EXCLUDE_KEYWORDS = re.compile(
    r"appartement|appart\b|studio|t[1-5]\b|f[1-5]\b|local|commerce|terrain seul|garage",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = criteres.get("departements", [])
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
        for dept in departements:
            dept_str = str(dept).zfill(2)
            slug = DEPT_SLUGS.get(dept_str)
            if not slug:
                continue
            try:
                biens = await _scrape_dept(
                    client, dept_str, slug, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[GreenAcres] Dept {dept_str}: {len(biens)} annonces")
            except Exception as e:
                print(f"[GreenAcres] Erreur dept {dept_str}: {e}")
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
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/immobilier/{slug}"
        if page > 1:
            url += f"?page={page}"

        r = await client.get(url, timeout=15)
        r.raise_for_status()

        page_biens = _parse_html(r.text, dept, prix_max, prix_min, surface_min)

        # Déduplication par advert-id
        new_biens = []
        for b in page_biens:
            aid = b.get("id_annonce", "")
            if aid and aid not in seen_ids:
                seen_ids.add(aid)
                new_biens.append(b)

        biens.extend(new_biens)

        if len(page_biens) < 20:
            break  # Dernière page (incomplète)

        await asyncio.sleep(0.6)

    return biens


def _parse_html(
    html: str,
    dept: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []

    for card in soup.select("div.announce-card"):
        if "skeleton" in card.get("class", []):
            continue

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
    # ── URL (base64 dans data-o) ──────────────────────────────────────────
    data_o = card.get("data-o", "")
    advert_id = card.get("data-advertid", "")
    url = ""
    if data_o:
        try:
            # Padding base64 robuste
            padded = data_o + "=" * (-len(data_o) % 4)
            url = base64.b64decode(padded).decode("utf-8", errors="ignore")
        except Exception:
            pass
    if not url and advert_id:
        url = f"{BASE_URL}/fr/properties/{advert_id}.htm"

    # ── Titre ────────────────────────────────────────────────────────────
    titre = card.get("title", "").strip()

    # Filtre type de bien : exclure appartements, inclure maisons/propriétés
    if titre:
        if _EXCLUDE_KEYWORDS.search(titre):
            return None
        # Si aucun mot-clé maison mais titre vide ou ambigu, on garde (propriétés de prestige)
    # Si le chemin URL contient 'appartement', on exclut
    if "appartement" in url.lower() or "apartment" in url.lower():
        return None

    # ── Prix ─────────────────────────────────────────────────────────────
    price_el = card.find("strong", class_="info-price")
    prix = _parse_price(price_el.get_text(strip=True) if price_el else "")
    if not prix:
        return None

    # ── Localisation ─────────────────────────────────────────────────────
    loc_el = card.find("div", class_="announce-localisation")
    loc_text = loc_el.get_text(" ", strip=True) if loc_el else ""
    # Format: "Fresnay-sur-Sarthe (Sarthe)" ou "Le Mans (Sarthe)"
    ville = ""
    code_postal = ""
    m_loc = re.match(r"^(.+?)\s*\(", loc_text)
    if m_loc:
        ville = m_loc.group(1).strip().title()

    # ── Caractéristiques ─────────────────────────────────────────────────
    chars_el = card.find("div", class_="characteristics")
    chars_text = chars_el.get_text(" ", strip=True) if chars_el else ""
    # "420 m² 5 000 m² de terrain 13 pièces 881 €/m²"
    # "505 m² 3,7 hectares de terrain 12 pièces 2 966 €/m²"

    surface = _parse_surface_hab(chars_text)
    surface_terrain = _parse_surface_terrain(chars_text)
    pieces = _parse_int(r"(\d+)\s*pi[eè]ces?", chars_text)

    # ── Photos ───────────────────────────────────────────────────────────
    photos = [
        img["data-thumb-src"]
        for img in card.select("[data-thumb-src]")
        if img.get("data-thumb-src")
    ][:PHOTOS_PER_CARD]

    if not titre:
        titre = f"Propriété Green-Acres — {ville}"

    return {
        "source": "greenacres",
        "url": url,
        "id_annonce": advert_id,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": chars_text,
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Green-Acres",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    """'370 000 €' ou '1 498 000 €' → float"""
    cleaned = re.sub(r"[€\s\xa0]", "", text).replace(",", ".")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface_hab(text: str) -> float | None:
    """Première occurrence de 'NNN m²' = surface habitable"""
    m = re.search(r"([\d\s\xa0]+)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            return float(val)
        except ValueError:
            pass
    return None


def _parse_surface_terrain(text: str) -> float | None:
    """'5 000 m² de terrain' ou '3,7 hectares de terrain' → m²"""
    # Hectares
    m_ha = re.search(r"([\d,\.]+)\s*hectares?\s+de\s+terrain", text, re.IGNORECASE)
    if m_ha:
        try:
            return float(m_ha.group(1).replace(",", ".")) * 10000
        except ValueError:
            pass
    # m² de terrain
    m_m2 = re.search(r"([\d\s\xa0]+)\s*m²\s+de\s+terrain", text, re.IGNORECASE)
    if m_m2:
        val = re.sub(r"[\s\xa0]", "", m_m2.group(1))
        try:
            return float(val)
        except ValueError:
            pass
    return None


# ── CLI standalone ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    biens = asyncio.run(
        search({
            "departements": criteres.departements[:4],
            "prix_max": criteres.prix_max,
            "prix_min": getattr(criteres, "prix_min", 0),
            "surface_min": criteres.surface_min,
        })
    )
    print(f"\nTotal Green-Acres: {len(biens)} annonces")
    for b in biens[:8]:
        print(
            f"  {b['titre'][:70]}"
            f" — {b['prix']}€"
            f" — {b.get('surface', '?')}m²"
            f" — {b.get('surface_terrain', '?')}m² terrain"
            f" — {b['ville']}"
        )

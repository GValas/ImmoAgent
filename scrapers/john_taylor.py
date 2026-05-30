"""scrapers/john_taylor.py — John Taylor France (agence de prestige internationale)

Méthode : scrape_simple (httpx) — SSR HTML classique
URL listing : https://www.john-taylor.fr/france/vente/  (pagination /pN/)
Inventaire France entier (~880 biens, ~24 pages) → POST-FILTRE par code_postal[:2].

Cards (chaque bien) :
  - URL    : a.link_property_view[href]
  - Loc    : meta[itemprop=availableAtOrFrom] content="Ville 06140"  (ville + CP)
  - Titre  : meta[itemprop=name] content="Vente Maison Vence"
  - Type   : ul.prod-bullets1 li (2e li = Maison / Appartement / Villa…)
  - Prix   : span[itemprop=price][content]  (ex content="945000.00")
  - ID     : ul.prod-bullets2 li (ex "V0545BX") ou suffixe de l'URL
  - Caract : span.list_icons small  → "127 m²" surface / terrain, "2 Chambres", "5 Pièces"
  - Photos : meta[itemprop=image] / img src
  - Desc   : div.desclist[itemprop=description]

NB : John Taylor ne couvre en France que des régions de prestige
     (Côte d'Azur, Paris, Provence, Sud-Ouest, Alpes, Rhône-Alpes, Normandie).
     Le post-filtre départemental garantit que seuls les biens dans les
     départements cibles sont renvoyés (souvent 0 pour le Val de Loire).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.john-taylor.fr"
LISTING_URL = f"{BASE_URL}/france/vente/"
MAX_PAGES = 30          # plafond sécurité (~24 pages réelles)
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# On exclut explicitement appartements / studios (on veut maisons/propriétés)
_EXCLUDE_TYPES = re.compile(r"appartement|studio|loft|duplex", re.IGNORECASE)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    all_cards = await _fetch_all_pages()

    results: list[dict] = []
    seen: set[str] = set()
    for card in all_cards:
        bien = _parse_card(card)
        if not bien:
            continue

        # ── POST-FILTRE DÉPARTEMENT (critique) ──────────────────────────────
        dept = bien.get("departement") or ""
        if departements and dept not in departements:
            continue

        # Filtres prix / surface
        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        # Exclure appartements
        if _EXCLUDE_TYPES.search(bien.get("type_bien", "")):
            continue

        aid = bien.get("id_annonce") or bien.get("url")
        if aid in seen:
            continue
        seen.add(aid)
        results.append(bien)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[JohnTaylor] Dept {dept}: {n} annonces")

    return results


async def _fetch_all_pages() -> list:
    """Récupère toutes les cartes de l'inventaire France (toutes pages)."""
    cards: list = []
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25) as client:
        for page in range(1, MAX_PAGES + 1):
            url = LISTING_URL if page == 1 else f"{LISTING_URL}p{page}/"
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    break
            except Exception as e:
                print(f"[JohnTaylor] Erreur page {page}: {e}")
                break

            soup = BeautifulSoup(r.text, "html.parser")
            page_cards = _extract_cards(soup)
            if not page_cards:
                break
            cards.extend(page_cards)
            await asyncio.sleep(0.5)

    return cards


def _extract_cards(soup: BeautifulSoup) -> list:
    """Chaque bien = bloc div[itemprop=offers] (Offer schema.org)."""
    return soup.select("div[itemprop=offers]")


def _parse_card(card) -> dict | None:
    try:
        # ── URL ─────────────────────────────────────────────────────────────
        link = card.select_one("a.link_property_view[href]") or card.select_one("a[itemprop=url][href]")
        url = link["href"].strip() if link else ""
        if not url:
            return None
        if url.startswith("/"):
            url = BASE_URL + url
        # On ne garde que les biens France (l'inventaire l'est déjà mais sécurité)
        if "/france/vente/" not in url:
            return None

        # ── ID ──────────────────────────────────────────────────────────────
        id_annonce = None
        id_el = card.select_one("ul.prod-bullets2 li")
        if id_el:
            id_annonce = id_el.get_text(strip=True)
        if not id_annonce:
            m = re.search(r"/(V[0-9A-Z]+)/?$", url)
            if m:
                id_annonce = m.group(1)

        # ── Ville + Code postal (meta availableAtOrFrom) ────────────────────
        ville = ""
        code_postal = ""
        loc_meta = card.select_one("meta[itemprop=availableAtOrFrom]")
        loc_txt = loc_meta.get("content", "").strip() if loc_meta else ""
        # ex: "Vence 06140"  /  "Saint-Tropez 83990"
        m_cp = re.search(r"(\d{5})", loc_txt)
        if m_cp:
            code_postal = m_cp.group(1)
            ville = loc_txt[: m_cp.start()].strip()
        else:
            ville = loc_txt
        departement = code_postal[:2] if code_postal else ""

        # ── Titre + type ────────────────────────────────────────────────────
        name_meta = card.select_one("meta[itemprop=name]")
        titre = name_meta.get("content", "").strip() if name_meta else ""
        type_bien = ""
        bullets = card.select("ul.prod-bullets1 li")
        if len(bullets) >= 2:
            type_bien = bullets[-1].get_text(strip=True).lower()
        if not type_bien and titre:
            m_t = re.search(r"vente\s+(\w+)", titre, re.IGNORECASE)
            if m_t:
                type_bien = m_t.group(1).lower()

        # ── Prix ────────────────────────────────────────────────────────────
        prix = None
        price_el = card.select_one("[itemprop=price]")
        if price_el:
            raw = price_el.get("content") or price_el.get_text(strip=True)
            prix = _parse_price(raw)

        # ── Caractéristiques (icônes) ───────────────────────────────────────
        surface = None
        surface_terrain = None
        chambres = None
        pieces = None
        for ic in card.select("span.list_icons"):
            img = ic.find("img")
            alt = (img.get("alt", "") if img else "").lower()
            small = ic.find("small")
            val_txt = small.get_text(" ", strip=True) if small else ""
            if alt.startswith("surface") and surface is None:
                surface = _parse_area(val_txt)
            elif alt.startswith("terrain") and surface_terrain is None:
                surface_terrain = _parse_area(val_txt)
            elif "chambre" in alt and chambres is None:
                chambres = _parse_int(val_txt)
            elif ("pièce" in alt or "piece" in alt) and pieces is None:
                pieces = _parse_int(val_txt)

        # ── Description ─────────────────────────────────────────────────────
        desc_el = card.select_one("div.desclist[itemprop=description]")
        description = desc_el.get_text(" ", strip=True) if desc_el else None

        # ── Photos ──────────────────────────────────────────────────────────
        photos: list[str] = []
        for im in card.select("meta[itemprop=image]"):
            src = im.get("content")
            if src and src not in photos:
                photos.append(src)
        for im in card.select("img[src]"):
            src = im.get("src", "")
            if src.startswith("http") and ".jpg" in src.lower() and src not in photos:
                photos.append(src)
        photos = photos[:PHOTOS_PER_CARD]

        if not titre:
            titre = f"John Taylor — {ville}".strip()

        return {
            "source": "john_taylor",
            "url": url,
            "id_annonce": id_annonce,
            "titre": titre[:150],
            "type_bien": type_bien or "maison",
            "description": description[:1200] if description else None,
            "departement": departement,
            "ville": ville[:80],
            "code_postal": code_postal,
            "surface": surface,
            "surface_terrain": surface_terrain,
            "pieces": pieces,
            "chambres": chambres,
            "prix": prix,
            "dpe": None,
            "photos": photos,
            "agence": "John Taylor",
        }
    except Exception:
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.,]", "", str(text))
    # "945000.00" ou "945 000" (espaces déjà retirés) ou "945000,00"
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        v = float(cleaned)
        return v if v > 0 else None
    except ValueError:
        return None


def _parse_area(text: str) -> float | None:
    """'127 m²' → 127.0"""
    m = re.search(r"([\d\s\xa0.,]+)\s*m", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
    try:
        return float(val)
    except ValueError:
        return None


def _parse_int(text: str) -> int | None:
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


# ── CLI standalone ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    biens = asyncio.run(
        search({
            "departements": criteres.departements,
            "prix_max": criteres.prix_max,
            "prix_min": getattr(criteres, "prix_min", 0),
            "surface_min": criteres.surface_min,
        })
    )
    print(f"\nTotal John Taylor (départements cibles): {len(biens)} annonces")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:60]}"
            f" — {b['prix']}€ — {b.get('surface', '?')}m² — {b['ville']} {b['code_postal']}"
        )

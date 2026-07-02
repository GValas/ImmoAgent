"""scrapers/nord_aveyron_immo.py — Nord Aveyron Immobilier (agence mono-département)

Méthode : scrape_simple (httpx) — SSR HTML (Apache, pas de Cloudflare).

Site : agence 100 % Aveyron (12) — maisons/villas, fermes en pierre, biens de
caractère Aubrac/Rouergue. Aucun filtre département dans l'URL (tout le stock
est dans le 12). On scrape la liste paginée puis on applique un post-filtre
STRICT code_postal[:2] == dept par sécurité → 0 fuite hors-zone.

URL pattern : /a-vendre/maisons-villas/{page}  (pagination ; page vide = 200 + 0 carte)

Cartes : article[itemscope][itemtype*="schema.org/Product"]  (~9/page)
  - Prix   : span[itemprop="price"]  (attribut content="580000")
  - Photo  : img[itemprop="image"][src]  (// → https:)
  - Titre  : h1[itemprop="name"]
  - Détail : h2  →  "Maison 360 m² - 17 Pièces - Laguiole (12210)"
  - Texte  : p[itemprop="description"]
  - Réf    : span[itemprop="productID"]  →  "Ref 4484"
  - URL    : a.btn-listing[href]  →  /3718-en-plein-centre-de-laguiole.html

Particularité : l'agence ne couvre QUE le département 12. Le scraper ne renvoie
donc des biens que si 12 figure dans `departements`. Sur les départements cibles
actuels (72, 28, 45, 89...) → 0 bien (mono-dept hors zone), comportement normal.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.nord-aveyron-immo.com"
LIST_PATH = "/a-vendre/maisons-villas"
MAX_PAGES = 15
PHOTOS_PER_CARD = 10

# Cette agence est 100 % Aveyron → un seul département couvert.
COVERED_DEPTS = {"12"}


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Agence mono-département : si aucun département couvert n'est demandé,
    # rien à scraper (évite des requêtes inutiles).
    cibles = [d for d in departements if d in COVERED_DEPTS]
    if not cibles:
        print(
            "[NordAveyron] Aucun département couvert (12) dans la cible → 0 annonce"
        )
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}{LIST_PATH}/{page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[NordAveyron] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                'article[itemtype*="schema.org/Product"]'
            )
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # Post-filtre dept STRICT (sécurité) : on n'accepte que la zone cible.
                cp = bien["code_postal"]
                dept = cp[:2] if cp else None
                if dept not in cibles:
                    continue
                bien["departement"] = dept

                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                seen_ids.add(aid)
                results.append(bien)
                new_on_page += 1

            if new_on_page == 0 and len(cards) == 0:
                break

            await asyncio.sleep(0.6)

    print(f"[NordAveyron] Total: {len(results)} annonces (dept {sorted(cibles)})")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a.btn-listing[href]") or card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Prix : attribut content prioritaire, sinon texte
    prix = None
    price_el = card.select_one('[itemprop="price"]')
    if price_el:
        content = price_el.get("content")
        prix = _parse_price(content) if content else _parse_price(
            price_el.get_text(" ", strip=True)
        )

    # Détail h2 : "Maison 360 m² - 17 Pièces - Laguiole (12210)"
    h2_el = card.select_one("h2")
    h2 = " ".join(h2_el.get_text(" ", strip=True).split()) if h2_el else ""
    ville, code_postal = _parse_loc(h2)
    surface = _parse_surface(h2)
    pieces = _parse_pieces(h2)
    type_bien = _parse_type(h2)

    # Titre
    title_el = card.select_one('h1[itemprop="name"]')
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Description
    desc_el = card.select_one('p[itemprop="description"]')
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Référence (id_annonce)
    ref_el = card.select_one('[itemprop="productID"]')
    ref = ref_el.get_text(strip=True) if ref_el else ""
    ref = re.sub(r"(?i)^ref\.?\s*", "", ref).strip()
    # id numérique du slug d'URL en secours : /3718-en-plein-centre-...
    id_num = ""
    m = re.search(r"/(\d+)-", url)
    if m:
        id_num = m.group(1)
    id_annonce = ref or id_num or url

    # Photos
    photos = []
    for img in card.select('img[itemprop="image"], img'):
        src = img.get("data-src") or img.get("src") or ""
        if not src or src.startswith("data:"):
            continue
        if src.startswith("//"):
            src = "https:" + src
        if src not in photos:
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "nord_aveyron_immo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else None,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Nord Aveyron Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Maison 360 m² - 17 Pièces - Laguiole (12210)' → ('Laguiole', '12210')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    # Ville = segment après le dernier séparateur " - ", avant le code postal.
    # (le h2 sépare ses champs par " - " ; les tirets internes des noms de
    #  commune — ex. Prades-D'Aubrac — sont ainsi préservés)
    ville = ""
    head = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    if " - " in head:
        ville = head.rsplit(" - ", 1)[-1].strip()
    else:
        ville = head.strip()
    return ville, cp


def _parse_price(text) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[€\s\xa0]", "", str(text)).replace(",", ".")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    m = re.search(r"([\d]+(?:[.,]\d+)?)\s*m²", text)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    return None


def _parse_pieces(text: str) -> int | None:
    m = re.search(r"(\d+)\s*Pi[eè]ces?", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_type(text: str) -> str:
    """Premier mot du h2 = type de bien (Maison, Villa, Ferme...)."""
    m = re.match(r"\s*([A-Za-zÀ-ÿ\-' ]+?)\s+\d", text)
    if m:
        return m.group(1).strip().lower()
    return "maison"


# ── CLI standalone ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    biens = asyncio.run(
        search(
            {
                "departements": criteres.departements,
                "prix_max": criteres.prix_max,
                "prix_min": getattr(criteres, "prix_min", 0),
                "surface_min": criteres.surface_min,
            }
        )
    )
    print(f"\nTotal Nord Aveyron Immo: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

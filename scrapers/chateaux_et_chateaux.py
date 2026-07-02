"""scrapers/chateaux_et_chateaux.py — Châteaux & Châteaux (prestige historique national)

Agence de prestige (châteaux, demeures historiques) à diffusion nationale. Prix
très élevés (souvent > 1 M€) → en pratique 0 stock sous 600 k€, mais couvre des
départements cibles (Cher 18, Maine-et-Loire 49…).

Méthode : scrape_simple (httpx) — SSR HTML (WordPress, plugin WPL). httpx pur 200,
pas de Playwright.

URL liste  : /?wplpage={N}   (page 1 = /?wplpage=1, ~2 pages, 8 cartes/page)

Cartes (liste) : div.wpl_prp_cont
  - URL       : a.view_detail[href]  (→ /properties/{id}-Chateau-{Ville}-{Dept}-France-…/)
  - id        : id "wpl_prp_cont{ID}"
  - Titre     : h3.wpl_prp_title
  - Localis.  : h4.wpl_prp_listing_location   ("Bourges, Cher, France")  ← contient le
                NOM du département (PAS de code postal)
  - Surface   : .built_up_area               ("7,000 m²")
  - Prix      : .price_box span              ("3,600,000 €" ou "Nous consulter")
  - Photos    : .wpl_gallery_image[data-src]

Filtre département : la carte n'expose AUCUN code postal, seulement
"Ville, Département, France". On extrait le nom de département de ce champ (et, en
secours, du slug d'URL) et on le matche contre core.dept_data.DEPT_NOMS — en testant
les noms COMPOSÉS ("Maine-et-Loire", "Eure-et-Loir", "Indre-et-Loire", "Loir-et-Cher")
AVANT les noms courts ambigus ("Indre", "Cher", "Loire") pour éviter les faux positifs.
Normalisation : minuscules, accents retirés, " et " / "-et-" unifiés, frontières de
mots. Post-filtre STRICT : département hors-zone OU indéterminé → bien EXCLU. 0 fuite.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import sys
import unicodedata
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.dept_data import DEPT_NOMS  # noqa: E402

BASE_URL = "https://www.chateauxetchateaux.com"
LIST_URL = f"{BASE_URL}/?wplpage={{page}}"
MAX_PAGES = 8
PHOTOS_PER_CARD = 10


def _norm(s: str) -> str:
    """Minuscule, sans accents, ' et '/'-et-' unifiés, espaces réduits."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[\s\-]+et[\s\-]+", " et ", s)
    s = re.sub(r"[\-']", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Table normalisée nom_dept → code, triée du nom LE PLUS LONG au plus court
# (les noms composés "indre et loire" testés AVANT "indre"/"loire" ambigus).
_DEPT_BY_NAME: list[tuple[str, str]] = sorted(
    ((_norm(nom), code) for code, nom in DEPT_NOMS.items()),
    key=lambda kv: len(kv[0]), reverse=True,
)


def _dept_from_text(text: str) -> str | None:
    """Cherche un nom de département (frontières de mots) dans `text` normalisé.
    Renvoie le code (ex '49') ou None. Teste les noms longs/composés en premier."""
    if not text:
        return None
    norm = _norm(text)
    for nom, code in _DEPT_BY_NAME:
        if re.search(r"(?<![a-z])" + re.escape(nom) + r"(?![a-z])", norm):
            return code
    return None


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            try:
                r = await client.get(LIST_URL.format(page=page))
            except Exception as e:
                print(f"[CEC] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break
            cards = BeautifulSoup(r.text, "html.parser").select("div.wpl_prp_cont")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                bien = _parse_card(card, departements)
                if not bien:
                    continue
                if bien["id_annonce"] in seen_ids:
                    continue
                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue
                seen_ids.add(bien["id_annonce"])
                results.append(bien)
                new_on_page += 1
            print(f"[CEC] Page {page}: {new_on_page} biens retenus (zone cible)")
            await asyncio.sleep(0.5)

    print(f"[CEC] Total : {len(results)} biens (zone cible)")
    return results


def _parse_card(card, departements: set[str]) -> dict | None:
    link = card.select_one("a.view_detail[href]")
    href = link.get("href", "") if link else ""
    if not href or "/properties/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    cid = card.get("id") or ""
    m = re.search(r"(\d+)", cid)
    id_annonce = m.group(1) if m else url

    title_el = card.select_one("h3.wpl_prp_title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    loc_el = card.select_one("h4.wpl_prp_listing_location")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    # "Bourges, Cher, France" → ville = 1er segment
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    ville = parts[0] if parts else ""

    # Département : depuis la localisation, secours sur le slug d'URL
    dept = _dept_from_text(loc)
    if not dept:
        slug = href.split("/properties/")[-1].replace("-", " ")
        dept = _dept_from_text(slug)
    if not dept or dept not in departements:
        return None  # post-filtre STRICT : hors zone / indéterminé → exclu (0 fuite)

    code_postal = None  # aucun CP exposé par la source

    # Surface (built_up_area : "7,000 m²")
    area_el = card.select_one(".built_up_area")
    surface = _parse_surface(area_el.get_text(" ", strip=True)) if area_el else None

    # Prix
    price_el = card.select_one(".price_box")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    desc_el = card.select_one(".wpl_prp_desc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Chambres depuis le slug d'URL si présent ("…-5-Chambres-…")
    chambres = None
    mc = re.search(r"(\d+)[- ]Chambres?", href, re.IGNORECASE)
    if mc:
        chambres = int(mc.group(1))

    photos: list[str] = []
    for img in card.select(".wpl_gallery_image"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:") and src not in photos:
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "chateaux_et_chateaux",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": "chateau",
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Châteaux & Châteaux",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    if not text or "consulter" in text.lower():
        return None
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = cleaned.replace(",", "")        # "53,000,000 €" → "53000000"
    cleaned = re.sub(r"[^\d].*$", "", cleaned)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    m = re.search(r"([\d][\d\s,\.]*?)\s*m", text or "")
    if not m:
        return None
    raw = m.group(1)
    raw = raw.replace(",", "")              # "7,000" → "7000"
    raw = re.sub(r"\s", "", raw)
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


# ── CLI standalone ────────────────────────────────────────────────────────────

if __name__ == "__main__":
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
    print(f"\nTotal Châteaux & Châteaux: {len(biens)} annonces")
    depts = sorted({(b.get('code_postal') or b.get('departement') or '')[:2] for b in biens})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b.get('departement')}] {b['titre'][:45]}"
            f" — {b['prix']}€ — {b.get('surface') or '?'}m² — {b['ville']}"
        )

"""scrapers/effectimmo.py — Effectimmo (petit réseau de mandataires)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress)
URL pattern : /biens-a-vendre/   (listing national unique)
              → AUCUN filtre département dans l'URL. La pagination /page/N/
                existe mais renvoie le MÊME listing (alias no-op : ~37 biens
                tiennent sur une seule page). On scrape donc le national puis
                on POST-FILTRE strictement sur code_postal[:2].

Cartes : div.section-list-property div.row > div   (colonne bootstrap)
  - URL   : a.card-link[href]  → /biens-a-vendre/{slug}/
  - Image : img.card-img-top[data-src]   (lazyload)
  - Titre : h4.card-title
  - Loc   : p.card-location  →  "Ville (CODEPOSTAL)"
  - Date  : p.card-date
  - Prix  : .info-property p.price  →  "298 500 € (HAI)"
  - Réf   : .info-property p.ref  →  "Référence: 1296"

Surface / pièces / chambres / terrain : ne figurent PAS dans la carte de liste
  (présents seulement dans le titre/description ou la page détail). On extrait ce
  qu'on peut du titre (ex. "de 64 m2", "4 chambres") et on laisse None sinon.

Type de bien : déduit du titre (maison / appartement / propriété…). On ne garde
  que maisons et propriétés (exclusion des appartements/terrains).

Couverture : réseau de mandataires à mandats sélectifs ; stock national très
  faible (~37 biens) et dispersé (84, 77, 56, 29, 75, 06, 44, 63, 74, 92, 94).
  AUCUNE présence dans la zone cible (72/28/45/89) au sondage 2026-06-09 →
  scraper fonctionnel mais 0 résultat sur la zone. Conservé pour réactivation
  si implantation future. Post-filtre CP[:2] → 0 fuite garantie.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int

BASE_URL = "https://effectimmo.com"
LIST_URL = f"{BASE_URL}/biens-a-vendre/"
PHOTOS_PER_CARD = 5


# Types de bien (mots du titre) à conserver : maisons / propriétés...
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|long[èe]re|manoir|ch[âa]teau|moulin|"
    r"demeure|domaine|mas|g[îi]te|bastide|corps de ferme|bourgeoise",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|studio|loft|hangar",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        try:
            r = await client.get(LIST_URL)
            if r.status_code != 200:
                print(f"[Effectimmo] HTTP {r.status_code} sur {LIST_URL}")
                return results
            cards = BeautifulSoup(r.text, "html.parser").select(
                "div.section-list-property div.row > div"
            )
        except Exception as e:
            print(f"[Effectimmo] Erreur listing : {e}")
            return results

    for card in cards:
        try:
            bien = _parse_card(card)
        except Exception:
            continue
        if not bien:
            continue

        cp = bien["code_postal"]
        dept = cp[:2] if cp else None
        # POST-FILTRE département strict : 0 fuite hors-zone
        if dept not in departements:
            continue

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

        bien["departement"] = dept
        seen_ids.add(aid)
        results.append(bien)

    print(f"[Effectimmo] {len(results)} annonces dans la zone "
          f"(sur {len(cards)} biens nationaux)")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a.card-link")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Titre
    title_el = card.select_one("h4.card-title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre and link:
        titre = link.get("title", "")

    # Filtrage type via titre : on ne garde que maisons / propriétés
    if _EXCLUDE_TYPE.search(titre) and not _KEEP_TYPE.search(titre):
        return None
    if not _KEEP_TYPE.search(titre):
        return None
    type_bien = _type_from_title(titre)

    # Localisation : "Ville (CODEPOSTAL)"
    loc_el = card.select_one("p.card-location")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)
    if not code_postal:
        return None

    # Prix
    price_el = card.select_one(".info-property p.price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Référence (id_annonce)
    ref_el = card.select_one(".info-property p.ref")
    ref = ""
    if ref_el:
        m = re.search(r"(\d+)", ref_el.get_text(strip=True))
        if m:
            ref = m.group(1)
    id_annonce = ref or url

    # Surface / chambres déduites du titre quand mentionnées
    surface = _parse_surface(titre)
    chambres = _parse_int(r"(\d+)\s*chambres?", titre)
    surface_terrain = _parse_terrain(titre)

    # Photos
    photos = []
    for img in card.select("img.card-img-top"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "effectimmo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": titre[:1200],
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Effectimmo",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _type_from_title(titre: str) -> str:
    m = _KEEP_TYPE.search(titre)
    if m:
        t = m.group(0).lower()
        if "maison" in t or "bourgeoise" in t:
            return "maison"
        return t
    return "maison"


def _parse_loc(text: str) -> tuple[str, str]:
    """'Plogoff (29770)' → ('Plogoff', '29770')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"\(HAI\)|\(FAI\)", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"[€\s\xa0]", "", cleaned)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'maison de 190 m2' → 190.0 (surface habitable plausible)."""
    if not text:
        return None
    # Évite de confondre avec 'terrain de NNN m2'
    for m in re.finditer(r"(\d[\d\s\xa0]*)\s*m2?", text, re.IGNORECASE):
        start = max(0, m.start() - 25)
        ctx = text[start:m.start()].lower()
        if "terrain" in ctx or "parcelle" in ctx:
            continue
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 1500:
                return f
        except ValueError:
            pass
    return None


def _parse_terrain(text: str) -> float | None:
    """'sur un terrain de 5619 m2' → 5619.0"""
    m = re.search(r"terrain[^0-9]{0,12}([\d\s\xa0]+)\s*m2?", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
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
        search(
            {
                "departements": criteres.departements,
                "prix_max": criteres.prix_max,
                "prix_min": getattr(criteres, "prix_min", 0),
                "surface_min": criteres.surface_min,
            }
        )
    )
    print(f"\nTotal Effectimmo : {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

"""scrapers/hsb_immobilier.py — HSB Immobilier (Horizon Sud Berry, agence Indre/Cher)

Méthode : scrape_simple (httpx) — SSR HTML (CMS maison, cartes a[data-item="bien"]).
URL pattern : /biens.html   (listing complet de l'agence, ~12 biens, une seule page).
              Le listing mélange plusieurs départements (Berry mais aussi qq biens
              hors-zone : 13, 15, 10...) → POST-FILTRE strict obligatoire. 0 fuite.

Cartes : a[data-item="bien"]
  - URL    : href  (ex: biens-492-maison-de-bourg.html)
  - Image  : .img img[data-src]   (src réel en data-src, lazy-load)
  - Nom    : .title .name
  - Réf    : .title .icon b  →  "Référence : ML2119"
  - Ville  : .title .state  →  "Urciers (36)"   (NN = code département, pas le CP)
  - Prix   : .price  →  "64 800€Honoraires..."
  - Résumé : .resume  →  contient souvent le CP complet "(36160)", la surface
             habitable et la surface terrain.

Département / code postal :
  - dept : code à 2 chiffres entre parenthèses dans .state (fiable, présent partout).
  - code_postal : 5 chiffres "(NNNNN)" extrait du résumé si présent (sinon "").
  Post-filtre : dept ∈ départements cibles.

Type de bien : depuis le nom (Maison / Ferme / Longère...). Exclut terrain seul.

Couverture : sud du Berry — Indre (36) et Cher (18), autour de La Châtre /
             Châteaumeillant. Petit stock mais profil rural de caractère.
             dernier_test 2026-06-09.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.hsbimmobilier.fr"
LISTING = "/biens.html"
PHOTOS_PER_CARD = 5


_KEEP_TYPE = re.compile(
    r"maison|villa|propriete|propriété|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|corps de ferme|pavillon|grange|"
    r"maison de (?:bourg|ville|village|campagne|caractère|maître)",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


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
        try:
            r = await client.get(BASE_URL + LISTING)
        except Exception as e:
            print(f"[HSB] Erreur listing: {e}")
            return results
        if r.status_code != 200:
            print(f"[HSB] Listing status {r.status_code}")
            return results

        cards = BeautifulSoup(r.text, "html.parser").select('a[data-item="bien"]')
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue

            # Post-filtre dept STRICT (listing multi-dept)
            if bien["departement"] not in departements:
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

            seen_ids.add(aid)
            results.append(bien)

    from collections import Counter

    for d, n in sorted(Counter(b["departement"] for b in results).items()):
        print(f"[HSB] Dept {d}: {n} annonces")

    return results


def _parse_card(card) -> dict | None:
    href = card.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

    name_el = card.select_one(".title .name")
    titre = name_el.get_text(" ", strip=True) if name_el else ""

    if _EXCLUDE_TYPE.search(titre) and not _KEEP_TYPE.search(titre):
        return None
    if not _KEEP_TYPE.search(titre):
        return None
    type_bien = titre.lower()

    # Ville + dept depuis .state "Urciers (36)"
    state_el = card.select_one(".title .state")
    state = state_el.get_text(" ", strip=True) if state_el else ""
    ville = re.sub(r"\s*\(\d{2}\)\s*$", "", state).strip()
    m_dept = re.search(r"\((\d{2})\)", state)
    dept = m_dept.group(1) if m_dept else None

    # Résumé : CP complet + surfaces
    resume_el = card.select_one(".resume")
    description = resume_el.get_text(" ", strip=True) if resume_el else ""

    code_postal = ""
    m_cp = re.search(r"\((\d{5})\)", description)
    if m_cp:
        code_postal = m_cp.group(1)
        if not dept:
            dept = code_postal[:2]
    if not dept:
        return None

    # Référence
    ref_el = card.select_one(".title .icon b")
    ref_txt = ref_el.get_text(" ", strip=True) if ref_el else ""
    m_ref = re.search(r"R[ée]f[ée]rence\s*:?\s*([\w-]+)", ref_txt, re.IGNORECASE)
    ref = m_ref.group(1) if m_ref else ""
    m_id = re.search(r"biens-(\d+)", href)
    id_annonce = ref or (m_id.group(1) if m_id else url)

    # Prix
    price_el = card.select_one(".price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Surfaces depuis le résumé
    surface = _parse_surface_hab(description)
    surface_terrain = _parse_terrain(description)

    # Photos
    photos = []
    for img in card.select(".img img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if not src.startswith("http"):
                src = f"{BASE_URL}/{src.lstrip('/')}"
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "hsb_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "HSB Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    # "64 800€Honoraires..." → garder les chiffres avant le 1er '€'
    head = text.split("€")[0] if "€" in text else text
    cleaned = re.sub(r"[^\d]", "", head)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface_hab(text: str) -> float | None:
    """Surface habitable totale.

    On cible des tournures de SURFACE GLOBALE (habitation / surface habitable /
    d'environ N m²), pas les surfaces de pièces ('salon de 14 m2'). À défaut de
    motif global fiable, on retourne None plutôt qu'une surface de pièce erronée.
    """
    if not text:
        return None
    patterns = [
        r"(?:surface\s+habitable|habitable)\s*(?:d['’e]\s*environ\s*)?[:\s]*"
        r"([\d\s\xa0]{2,}(?:[.,]\d+)?)\s*m[²2]",
        r"(?:maison|habitation|propri[ée]t[ée]|longere|longère|ferme|demeure)"
        r"[^.]*?d['’]?\s*environ\s+([\d\s\xa0]{2,}(?:[.,]\d+)?)\s*m[²2]",
        r"(?:maison|habitation)\s+de\s+([\d\s\xa0]{2,}(?:[.,]\d+)?)\s*m[²2]",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
            try:
                f = float(val)
                if 30 <= f <= 2000:
                    return f
            except ValueError:
                pass
    return None


def _parse_terrain(text: str) -> float | None:
    """'terrain arboré de 1 750 m2' → 1750 ; 'sur N hectares' → N*10000."""
    if not text:
        return None
    m = re.search(r"terrain[^.]*?de\s+([\d\s\xa0]+(?:[.,]\d+)?)\s*(ha|hectares?|m[²2])",
                  text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
        try:
            f = float(val)
            if m.group(2).lower().startswith(("ha", "hect")):
                return round(f * 10000)
            return f
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
    print(f"\nTotal HSB Immobilier: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['ville']} ({b['code_postal']})"
        )

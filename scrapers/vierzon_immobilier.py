"""scrapers/vierzon_immobilier.py — Vierzon Immobilier (agence indépendante, Cher 18)

Agence indépendante basée à Vierzon (18100) couvrant le Cher et ses alentours.
Site SSR (CMS « Databimmo ») : toutes les annonces sont dans le HTML brut, pas de
JS requis → httpx pur.

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /biens.html  (page unique, ~36 cartes — pas de pagination réelle,
              max=1 sur le sélecteur de page au moment du test)
              → AUCUN filtre département côté serveur (agence mono-secteur Cher).
                Filtre dept = POST-FILTRE STRICT sur le code postal de la carte.

Filtre département (0 fuite) :
  - chaque carte expose la localisation dans .state → "Ville (18 100)" ;
  - on en extrait le code postal (espaces retirés) puis CP[:2] → dept ;
  - on n'accepte la carte que si CP[:2] ∈ départements cibles.

Cartes : a[data-item="bien"]
  - URL    : a[data-item=bien][href]  → biens-{id}-{slug}.html
  - Titre  : .name
  - Loc    : .state  → "Ville (18 100)"
  - Texte  : .resume (description)
  - Datas  : .datas span (chacun = surface / terrain / pièces / chambres,
             reconnus au mot-clé : "pièces", "chambres", sinon m² → surface puis terrain)
  - Prix   : .price .now  → "525 000€"
  - Photo  : .img img[data-src]

Type de bien : déduit du titre/résumé ; on ne garde que maisons / propriétés /
               fermes…, on exclut appartements / terrains / locaux / immeubles.

Couverture : Cher (18) uniquement (les autres départements cibles → 0 bien, normal).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.vierzon-immobilier-18.com"
LIST_PATH = "/biens.html"
PHOTOS_PER_CARD = 6


# Types de bien à conserver : maisons / propriétés / fermes...
_KEEP_TYPE = re.compile(
    r"maison|villa|propri[eé]t[eé]|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|mas|g[iî]te|corps de ferme|maison de village|pavillon|"
    r"bourg|campagne|b[aâ]tisse",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|viager|hangar|b[aâ]timent industriel|studio|loft",
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
            r = await client.get(f"{BASE_URL}{LIST_PATH}")
        except Exception as e:
            print(f"[VierzonImmo] Erreur réseau : {e}")
            return []
        if r.status_code != 200:
            print(f"[VierzonImmo] HTTP {r.status_code}")
            return []

        cards = BeautifulSoup(r.text, "html.parser").select("a[data-item='bien']")
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue

            # POST-FILTRE STRICT — 0 fuite hors-zone
            cp = bien["code_postal"]
            if not cp or cp[:2] not in departements:
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

    print(f"[VierzonImmo] {len(results)} annonces (depts {sorted({b['departement'] for b in results}) or '∅'})")
    return results


def _parse_card(card) -> dict | None:
    href = card.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

    m_id = re.search(r"biens-(\d+)-", href)
    id_annonce = m_id.group(1) if m_id else url

    name_el = card.select_one(".name")
    titre = name_el.get_text(" ", strip=True) if name_el else ""

    resume_el = card.select_one(".resume")
    description = resume_el.get_text(" ", strip=True) if resume_el else ""

    # Type de bien : titre puis résumé
    blob = f"{titre} {description}"
    if _EXCLUDE_TYPE.search(blob) and not _KEEP_TYPE.search(titre):
        return None
    if not _KEEP_TYPE.search(blob):
        return None
    type_bien = _deduce_type(titre) or "maison"

    # Localisation : ".state" → "Ville (18 100)"
    state_el = card.select_one(".state")
    loc = state_el.get_text(" ", strip=True) if state_el else ""
    ville, code_postal = _parse_loc(loc)

    # Datas : surface / terrain / pièces / chambres
    surface = surface_terrain = None
    pieces = chambres = None
    for sp in card.select(".datas span"):
        txt = sp.get_text(" ", strip=True)
        low = txt.lower()
        if "chambre" in low:
            chambres = _first_int(txt)
        elif "pièce" in low or "piece" in low:
            pieces = _first_int(txt)
        elif "m²" in low or "m2" in low:
            val = _parse_m2(txt)
            if surface is None:
                surface = val
            elif surface_terrain is None:
                surface_terrain = val

    price_el = card.select_one(".price .now")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    photos = []
    for img in card.select(".img img, img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif not src.startswith("http"):
                src = f"{BASE_URL}/{src.lstrip('/')}"
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "vierzon_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Vierzon Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _deduce_type(text: str) -> str:
    m = _KEEP_TYPE.search(text or "")
    return m.group(0).lower() if m else ""


def _parse_loc(text: str) -> tuple[str, str]:
    """'Vierzon (18 100)' → ('Vierzon', '18100')"""
    cp = ""
    m = re.search(r"\((\d[\d\s]{3,7})\)", text)
    if m:
        cp = re.sub(r"\s", "", m.group(1))[:5]
    ville = re.sub(r"\s*\([\d\s]+\)\s*$", "", text).strip()
    ville = re.sub(r"\s+", " ", ville)
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    if v and v < 1000:
        return None
    return v


def _first_int(text: str) -> int | None:
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def _parse_m2(text: str) -> float | None:
    m = re.search(r"([\d\s\xa0]+)\s*m", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 1 <= f <= 100000:
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
    print(f"\nTotal Vierzon Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

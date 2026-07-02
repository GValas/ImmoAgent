"""scrapers/immo_reseau.py — Immo Réseau (réseau national de conseillers indépendants)

Méthode : scrape_simple (httpx) — SSR WordPress (thème immoreseau).

Listing national paginé : /achats/ puis /achats/page/{N}/ (24 cartes/page, ~30 pages
≈ 720 annonces). Le moteur de recherche du site est en POST/JS et les paramètres GET
de département sont IGNORÉS côté serveur → on parcourt le listing national et on
POST-FILTRE par code_postal[:2] (le CP est exposé en clair dans le titre de carte
"Maison à VILLE (CP)"), comme remax/era/groupe_mercure. 0 fuite.

Cartes : div.card.card-annonce
  - URL    : a[href$='/achats/annonce-{id}/']
  - Titre  : h2  →  "Maison à ARGENVIERES (18140)"
  - Texte  : "... 92 500 € (881€/m²) ... 105 m² 941 m² Classe énergétique : E"
             (1er m² = surface habitable, 2e m² = terrain)
  - Photo  : img[src] wp-content/uploads (on ignore les icônes svg du thème)

Type : déduit du titre (maison/villa/propriété…). On ne garde que maisons/propriétés.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.immo-reseau.com"
LISTING = BASE_URL + "/achats/"
MAX_PAGES = 35
PHOTOS_PER_CARD = 6


_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|mas|g[iî]te|corps de ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"studio|loft",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = LISTING if page == 1 else f"{LISTING}page/{page}/"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[ImmoReseau] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.card.card-annonce")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                bien = _parse_card(card)
                if not bien:
                    continue
                if bien["id_annonce"] in seen_ids:
                    continue

                dept = bien["code_postal"][:2] if bien["code_postal"] else ""
                if dept not in departements:
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
                bien["departement"] = dept
                results.append(bien)
                new_on_page += 1

            await asyncio.sleep(0.4)

    # Log par département pour cohérence avec les autres scrapers
    from collections import Counter

    dist = Counter(b["departement"] for b in results)
    for dept in sorted(departements):
        print(f"[ImmoReseau] Dept {dept}: {dist.get(dept, 0)} annonces")

    return results


def _parse_card(card) -> dict | None:
    link = card.find("a", href=re.compile(r"/achats/annonce-(\d+)/"))
    if not link:
        return None
    href = link["href"]
    url = href if href.startswith("http") else BASE_URL + href
    m_id = re.search(r"/achats/annonce-(\d+)/", href)
    id_annonce = m_id.group(1) if m_id else url

    h2 = card.find(["h2", "h3"])
    titre = h2.get_text(" ", strip=True) if h2 else ""
    if not titre:
        return None

    # Type filter from title
    if _EXCLUDE_TYPE.search(titre) and not _KEEP_TYPE.search(titre):
        return None
    if not _KEEP_TYPE.search(titre):
        return None
    type_bien = _detect_type(titre)

    # "Maison à ARGENVIERES (18140)" → ville + CP
    ville, code_postal = _parse_loc(titre)
    if not code_postal:
        return None

    full = card.get_text(" ", strip=True)

    prix = _parse_price(full)
    surface, surface_terrain = _parse_surfaces(full)
    dpe = _parse_dpe(full)

    photos = []
    for img in card.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        if "wp-content/uploads" in src and not src.startswith("data:"):
            # Image pleine taille : retirer le suffixe -WxH
            src = re.sub(r"-\d+x\d+(\.\w+)$", r"\1", src)
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "immo_reseau",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Immo Réseau",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _detect_type(titre: str) -> str:
    t = titre.lower()
    for kw in ("château", "chateau", "manoir", "moulin", "longère", "longere",
               "ferme", "villa", "propriété", "propriete", "demeure", "domaine"):
        if kw in t:
            return kw.replace("chateau", "château").replace("propriete", "propriété")
    return "maison"


def _parse_loc(text: str) -> tuple[str, str]:
    """'Maison à ARGENVIERES (18140)' → ('Argenvieres', '18140')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    m_ville = re.search(r"\b[àa]\s+(.+?)\s*\(\d{5}\)", text)
    ville = m_ville.group(1).strip() if m_ville else ""
    if ville:
        ville = ville.title()
    return ville, cp


def _parse_price(text: str) -> float | None:
    # Prix principal : premier "NNN NNN €" hors "€/m²"
    for m in re.finditer(r"([\d][\d\s\xa0]{2,})\s*€", text):
        chunk = text[m.start(): m.end() + 4]
        if "/m" in chunk:
            continue
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if f > 1000:
                return f
        except ValueError:
            pass
    return None


def _parse_surfaces(text: str) -> tuple[float | None, float | None]:
    """1er 'NNN m²' = habitable, 2e = terrain (heuristique du thème)."""
    nums = []
    for m in re.finditer(r"([\d][\d\s\xa0]*)\s*m²", text):
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            nums.append(float(val))
        except ValueError:
            pass
    surface = nums[0] if nums else None
    terrain = nums[1] if len(nums) > 1 else None
    if surface and not (8 <= surface <= 3000):
        surface = None
    return surface, terrain


def _parse_dpe(text: str) -> str | None:
    m = re.search(r"Classe\s+[ée]nerg[ée]tique\s*:?\s*([A-G])\b", text, re.IGNORECASE)
    return m.group(1).upper() if m else None


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
    print(f"\nTotal Immo Réseau: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — DPE {b.get('dpe') or '?'} — {b['type_bien']}"
        )

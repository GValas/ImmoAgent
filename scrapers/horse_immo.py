"""scrapers/horse_immo.py — Horse Immo (immobilier équestre national)

Méthode : scrape_simple (httpx) — SSR WordPress (thème custom + Search & Filter Pro).
Stock curated/restreint, propriétés équestres (haras, écuries, domaines, manoirs,
longères avec installations chevaux) sur toute la France.

Listing : https://www.horse-immo.fr/proprietes.html
Filtre département CÔTÉ SERVEUR (vérifié : aucune fuite hors-dept) :
    ?_sfm_departement={NN}     (Search & Filter Pro, valeur = code dept "45", "37"…)
Pagination (rarement utile, stock faible) : &sf_paged={N}  (20/page)

Cartes : a.proprieteslist__item__link  (le lien EST la carte)
  - URL    : href  → /proprietes/{slug}.html
  - Titre  : .proprieteslist__item__title
  - Dept   : .proprieteslist__item__meta--departement   ("45")   ← filtre + dept
  - Terrain: .proprieteslist__item__meta--surface_terrain ("9 hectares" / "1.8659 hectares")
  - Surface: .proprieteslist__item__meta--surface_habitable ("300.00 m²")
  - Prix   : .proprieteslist__item__meta--prix            ("2 440 000,00 €")
  - Boxes  : .proprieteslist__item__meta--nombre_box      ("14 boxes")
  - Photo  : img[data-lazy-src]

Limite : le site n'expose PAS de ville ni de code postal (ni sur la liste ni sur la
fiche) — seulement le département + la région. On renseigne donc `departement` (fiable,
issu de la carte) et on laisse `code_postal`/`ville` vides. Le filtre se fait sur le
département serveur ET un contrôle de sécurité sur la valeur de la carte.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.horse-immo.fr"
LISTING_URL = f"{BASE_URL}/proprietes.html"
MAX_PAGES = 6           # plafond de sécurité (stock réel < 20/dept)
PHOTOS_PER_CARD = 1     # 1 photo de couverture dispo sur la liste


# On exclut les biens purement terrain / commerce (rare ici, mais on garde maisons,
# domaines, propriétés, haras, écuries, longères, manoirs…).
_EXCLUDE_TYPE = re.compile(r"\bterrain\b|garage|parking|local commercial", re.IGNORECASE)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            try:
                biens = await _scrape_dept(
                    client, dept, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[HorseImmo] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[HorseImmo] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.5)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        params = {"_sfm_departement": dept}
        if page > 1:
            params["sf_paged"] = page
        r = await client.get(LISTING_URL, params=params)
        if r.status_code != 200:
            break

        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("a.proprieteslist__item__link")
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            bien = _parse_card(card, dept)
            if not bien:
                continue

            # Sécurité anti-fuite : on n'accepte que le département cible.
            if bien["departement"] != dept:
                continue

            aid = bien["id_annonce"]
            if aid in seen:
                continue

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            seen.add(aid)
            biens.append(bien)
            new_on_page += 1

        # Pas de lien vers la page suivante → on arrête
        if not soup.select_one(f'a[href*="sf_paged={page + 1}"]'):
            break
        if new_on_page == 0:
            break

        await asyncio.sleep(0.4)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    href = card.get("href", "")
    if not href or "/proprietes/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Département (valeur brute de la carte)
    dept_el = card.select_one(".proprieteslist__item__meta--departement")
    card_dept = dept_el.get_text(strip=True) if dept_el else ""
    card_dept = re.sub(r"\D", "", card_dept)[:2] if card_dept else dept

    # Titre
    title_el = card.select_one(".proprieteslist__item__title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    if _EXCLUDE_TYPE.search(titre):
        return None

    # id annonce depuis le slug
    slug = href.rstrip("/").split("/")[-1].replace(".html", "")
    id_annonce = slug or url

    # Prix : "2 440 000,00 €"
    prix_el = card.select_one(".proprieteslist__item__meta--prix")
    prix = _parse_price(prix_el.get_text(" ", strip=True)) if prix_el else None

    # Surface habitable : "300.00 m²"
    surf_el = card.select_one(".proprieteslist__item__meta--surface_habitable")
    surface = _parse_surface(surf_el.get_text(" ", strip=True)) if surf_el else None

    # Surface terrain : "9 hectares" / "1.8659 hectares" → m²
    terr_el = card.select_one(".proprieteslist__item__meta--surface_terrain")
    surface_terrain = (
        _parse_hectares(terr_el.get_text(" ", strip=True)) if terr_el else None
    )

    # Photo de couverture
    photos = []
    img = card.select_one("img")
    if img:
        src = img.get("data-lazy-src") or img.get("src") or ""
        if src.startswith("http"):
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "horse_immo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "propriété équestre",
        "description": "",
        "departement": card_dept,
        "ville": "",            # non exposé par le site
        "code_postal": "",      # non exposé par le site
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "dpe": None,
        "photos": photos,
        "agence": "Horse Immo",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    """'2 440 000,00 €' → 2440000.0"""
    cleaned = text.replace("\xa0", " ")
    cleaned = re.sub(r"[^\d,\.]", "", cleaned.replace(" ", ""))
    # format fr : virgule = décimale → on coupe avant les centimes
    cleaned = cleaned.split(",")[0].replace(".", "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'300.00 m²' → 300.0"""
    m = re.search(r"([\d\s\xa0]+(?:[.,]\d+)?)\s*m", text)
    if not m:
        return None
    val = m.group(1).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        f = float(val)
        return f if 5 <= f <= 5000 else None
    except ValueError:
        return None


def _parse_hectares(text: str) -> float | None:
    """'9 hectares' / '1.8659 hectares' → m² (float). '13.763 hectares' → 137630."""
    m = re.search(r"([\d\s\xa0]+(?:[.,]\d+)?)\s*hectare", text, re.IGNORECASE)
    if m:
        val = m.group(1).replace("\xa0", "").replace(" ", "").replace(",", ".")
        try:
            return round(float(val) * 10000, 0)
        except ValueError:
            return None
    # fallback : valeur en m² ?
    m2 = re.search(r"([\d\s\xa0]+)\s*m", text)
    if m2:
        val = m2.group(1).replace("\xa0", "").replace(" ", "")
        try:
            return float(val)
        except ValueError:
            return None
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
    print(f"\nTotal Horse Immo: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    # contrôle de fuite
    cibles = {str(d).zfill(2) for d in criteres.departements}
    fuites = [b for b in biens if b["departement"] not in cibles]
    print(f"Fuites hors-dept : {len(fuites)}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']}"
        )

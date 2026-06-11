"""scrapers/ajp_immobilier.py — AJP Immobilier (réseau ~110 agences, plateforme immo-facile)

Méthode : scrape_simple (httpx) — SSR HTML (cartes présentes dans le HTML brut).

Couverture réseau : Bretagne / Pays de la Loire / Nouvelle-Aquitaine / Paris
(depts 17, 33, 35, 40, 44, 49, 56, 64, 75, 85). Dans la ZONE CIBLE du projet,
seul le Maine-et-Loire (49) est couvert par des agences ; les pages 49 débordent
parfois sur la Mayenne (53) limitrophe (Segré) — 53 est aussi un département cible,
donc on le garde. Tout le reste est hors-zone et écarté par le post-filtre.

URL pattern (recherche par agence/ville, paramètres immo-facile) :
    /immobilier/achat/immo-{ville}-49?perPage=200&page=N
  → cartes SSR ; pas de filtre département serveur, mais chaque carte porte
    son département dans l'URL détail → post-filtre STRICT sur ce département.

Pages "ville" de la zone cible (couvrent tout le 49 + débordement 53) :
    angers-49, cholet-49, segre-49, sevremoine-49

Cartes : div.property-card
  - onclick="window.location='.../annonces/achat/{type}/{ville}-{NN}/{id}'"
        → type de bien, ville-slug, DÉPARTEMENT (NN), id_annonce
  - .card-body :
        a (1er)            → type de bien ("Maison", "Appartement"…)
        span (à côté)      → prix ("287 800 €")
        a.font-weight-bold → ville ("le plessis grammoire")
        .amount + icône    → surface (dimensions-icon), terrain (terrain-icon),
                             chambres (bedrooms-icon), sdb (bathrooms-icon)
  - img.carousel-item__img[data-src] → photos (média immo-facile.com)
  - .small[title]          → adresse/description courte

DPE / description complète : récupérés ensuite par gallery.py (page détail).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.ajp-immobilier.com"
MAX_PAGES = 6
PER_PAGE = 200
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Pages "ville" du réseau situées dans la zone cible (Maine-et-Loire).
# Elles couvrent tout le 49 (agences Angers/Cholet/Segré/Sèvremoine) et
# débordent un peu sur le 53 limitrophe — les deux sont des départements cibles.
CITY_PAGES: list[str] = [
    "angers-49",
    "cholet-49",
    "segre-49",
    "sevremoine-49",
]

# Types d'URL détail à conserver (maisons / propriétés). On exclut explicitement
# appartement / terrain / immeuble / local…
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"viager|parking",
    re.IGNORECASE,
)

_DETAIL_RE = re.compile(
    r"/annonces/achat/([a-z-]+)/([a-z0-9-]+)-(\d{2})/(\d+)", re.IGNORECASE
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    dept_set = set(departements)
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Si aucun des départements couverts par le réseau (49/53) n'est ciblé,
    # rien à faire.
    if not ({"49", "53"} & dept_set):
        print("[AJP] Aucun département couvert (49/53) dans la cible — skip")
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for city in CITY_PAGES:
            try:
                biens = await _scrape_city(
                    client, city, dept_set, prix_max, prix_min, surface_min, seen_ids
                )
                results.extend(biens)
                print(f"[AJP] {city}: {len(biens)} annonces retenues")
            except Exception as e:
                print(f"[AJP] Erreur {city}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_city(
    client: httpx.AsyncClient,
    city: str,
    dept_set: set[str],
    prix_max: int,
    prix_min: int,
    surface_min: int,
    seen_ids: set[str],
) -> list[dict]:
    biens: list[dict] = []

    for page in range(1, MAX_PAGES + 1):
        url = (
            f"{BASE_URL}/immobilier/achat/immo-{city}"
            f"?perPage={PER_PAGE}&page={page}"
        )
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select("div.property-card")
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

            dept = bien["departement"]
            # POST-FILTRE DÉPARTEMENT STRICT : on n'accepte que la zone cible.
            if dept not in dept_set:
                continue
            # Cohérence CP si présent.
            if bien["code_postal"] and bien["code_postal"][:2] != dept:
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
            biens.append(bien)
            new_on_page += 1

        # perPage élevé → tout sur la 1re page en général. Pas de nouveaux biens
        # (ou page vide) → on arrête.
        if new_on_page == 0:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card) -> dict | None:
    # URL détail via onclick (window.location) ou via les <a> du corps.
    onclick = card.get("onclick", "") or ""
    m = _DETAIL_RE.search(onclick)
    if not m:
        a = card.select_one("a[href*='/annonces/achat/']")
        if a:
            m = _DETAIL_RE.search(a.get("href", ""))
    if not m:
        return None

    type_seg, ville_slug, dept, id_annonce = m.groups()
    url = f"{BASE_URL}/annonces/achat/{type_seg}/{ville_slug}-{dept}/{id_annonce}"

    # Filtre type de bien (URL).
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg.replace("-", " ").strip() or "maison"

    body = card.select_one(".card-body")
    if body is None:
        body = card

    # Type (1er <a>) et prix (<span> voisin) dans le 1er bloc flex.
    titre_type_el = body.select_one("a.capitalize")
    titre_type = (
        titre_type_el.get_text(" ", strip=True) if titre_type_el else type_bien.title()
    )

    prix = None
    head = body.select_one(".d-flex.justify-content-between")
    if head:
        span = head.select_one("span")
        if span:
            prix = _parse_price(span.get_text(" ", strip=True))
    if prix is None:
        # secours : 1er span contenant '€'
        for span in body.select("span"):
            if "€" in span.get_text():
                prix = _parse_price(span.get_text(" ", strip=True))
                break

    # Ville (lien en gras).
    ville_el = body.select_one("a.font-weight-bold")
    ville = ville_el.get_text(" ", strip=True) if ville_el else ville_slug.replace("-", " ")
    ville = ville.strip().title()

    # Surfaces / chambres : repérées par l'icône associée.
    surface = surface_terrain = None
    chambres = None
    for block in body.select("div.d-flex.align-items-center"):
        img = block.select_one("img")
        amount_el = block.select_one(".amount")
        if not img or not amount_el:
            continue
        icon = (img.get("data-src") or img.get("src") or "").lower()
        val_txt = amount_el.get_text(" ", strip=True)
        if "dimensions-icon" in icon:
            surface = _parse_metric(val_txt)
        elif "terrain-icon" in icon:
            surface_terrain = _parse_metric(val_txt)
        elif "bedrooms-icon" in icon:
            chambres = _parse_int(val_txt)

    # Description courte (attribut title du bloc adresse, sinon texte).
    desc_el = body.select_one(".small[title]")
    description = ""
    if desc_el:
        description = (desc_el.get("title") or desc_el.get_text(" ", strip=True) or "").strip()

    # Code postal : non exposé en liste (ville-slug seulement) → None,
    # le département vient de l'URL (fiable). gallery.py/geolocate.py affineront.
    code_postal = ""

    titre = f"{titre_type} {ville}".strip()

    # Photos (carrousel immo-facile).
    photos: list[str] = []
    for img in card.select("img.carousel-item__img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "ajp_immobilier",
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
        "pieces": None,            # non exposé en liste ; gallery.py peut compléter
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "AJP Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text.replace("\xa0", " "))
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    # garde-fou : un prix d'immobilier plausible (évite de capter un "4" isolé)
    if v is not None and v < 1000:
        return None
    return v


def _parse_metric(text: str) -> float | None:
    """'103 m²' → 103.0"""
    m = re.search(r"([\d\s\xa0]+)", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        return float(val) if val else None
    except ValueError:
        return None


def _parse_int(text: str) -> int | None:
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


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
    print(f"\nTotal AJP Immobilier: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

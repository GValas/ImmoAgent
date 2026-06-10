"""scrapers/luxuryestate.py — LuxuryEstate.com (agrégateur international de prestige)

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /france/{region-slug}/{dept-slug}?pag={N}
              ex : /france/centre/indre-and-loire
                   /france/pays-de-la-loire/sarthe?pag=2
              → filtre département CÔTÉ SERVEUR (une URL par département cible).

Particularités :
  - Site anglophone (le projet est FR, mais ce portail expose ses libellés en EN).
  - Agrégateur de biens de prestige : prix souvent très élevés → le filtre
    `prix_max` écarte une bonne partie de l'inventaire (comportement attendu).
  - Le code postal n'est PAS exposé dans la carte (seulement Ville + Département
    en clair). Le post-filtre département s'appuie donc sur le TOPONYME : le titre
    se termine par le nom du département (ex. « …, Sarthe », « …, Indre and Loire »),
    re-vérifié contre le nom attendu pour l'URL → 0 fuite hors-zone.

Cartes : li.search-list__item  (data-id = id de l'annonce)
  - URL    : a.details_title[href]  → /p{ID}-{type}-for-sale-{ville}
  - Titre  : a.details_title  → "Luxury home in Tours, Indre and Loire"
             (type + ' in ' + Ville + ', ' + Département)
  - Prix   : div.price  → "€ 1,395,000"
  - Specs  : div.specs  → "328 m²  5  6"  (surface, salles de bain, chambres)
  - Photos : div.foto img[src]  (// → https:)
  - Agence : .listed-by span / .agency img[alt]

Type de bien : déduit du préfixe du titre (Luxury home / Villa / Castle /
               Rural or Farmhouse / Apartment…). On exclut les appartements.

Pagination : ?pag={N} (jusqu'à ~21 pages selon le département, ~14-15 cartes/page).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import unicodedata

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.luxuryestate.com"
MAX_PAGES = 22
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

# Code département → slug URL luxuryestate.com (region/departement)
DEPT_SLUGS: dict[str, str] = {
    "72": "pays-de-la-loire/sarthe",
    "49": "pays-de-la-loire/maine-et-loire",
    "53": "pays-de-la-loire/mayenne",
    "28": "centre/eure-et-loir",
    "45": "centre/loiret",
    "37": "centre/indre-and-loire",
    "36": "centre/indre",
    "18": "centre/cher",
    "41": "centre/loir-et-cher",
    "89": "bourgogne-franche-comte/yonne",
    "58": "bourgogne-franche-comte/nievre",
}

# Nom du département tel qu'il apparaît en fin de titre (toponyme) → post-filtre.
# (luxuryestate anglicise « Indre-et-Loire » en « Indre and Loire » ; les autres
#  gardent la forme française.)
DEPT_LABELS: dict[str, str] = {
    "72": "sarthe",
    "49": "maine-et-loire",
    "53": "mayenne",
    "28": "eure-et-loir",
    "45": "loiret",
    "37": "indre and loire",
    "36": "indre",
    "18": "cher",
    "41": "loir-et-cher",
    "89": "yonne",
    "58": "nievre",
}

# Préfixes de type à exclure (appartements, terrains, commerces…)
_EXCLUDE_TYPE = re.compile(
    r"apartment|appartement|loft|penthouse|land|terrain|office|commercial|"
    r"garage|parking|building|shop|warehouse",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    """minuscule, sans accents, espaces compactés (pour comparer toponymes)."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, slug, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[LuxuryEstate] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[LuxuryEstate] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

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
    label = _norm(DEPT_LABELS.get(dept, ""))

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/france/{slug}"
        if page > 1:
            url += f"?pag={page}"
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select("li.search-list__item")
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            try:
                bien = _parse_card(card, dept)
            except Exception:
                continue
            if not bien:
                continue

            # Post-filtre département STRICT par toponyme : le titre doit se
            # terminer par le nom du département attendu → 0 fuite hors-zone.
            if label and label not in _norm(bien.get("_dept_label") or ""):
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

            bien.pop("_dept_label", None)
            seen_ids.add(aid)
            biens.append(bien)
            new_on_page += 1

        if new_on_page == 0:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a.details_title")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    title_raw = link.get_text(" ", strip=True)
    # "Luxury home in Tours, Indre and Loire" → type / ville / dept-label
    type_part = title_raw
    ville = ""
    dept_label = ""
    if "," in title_raw:
        before, dept_label = title_raw.rsplit(",", 1)
        dept_label = dept_label.strip()
    else:
        before = title_raw
    m = re.search(r"^(.*?)\bin\b(.*)$", before, re.IGNORECASE)
    if m:
        type_part = m.group(1).strip()
        ville = m.group(2).strip()
    else:
        ville = before.strip()

    type_norm = type_part.lower()
    if _EXCLUDE_TYPE.search(type_norm):
        return None
    type_bien = type_part or "maison"

    # id_annonce : data-id de la carte, sinon /p{ID}- de l'URL
    aid = card.get("data-id") or ""
    if not aid:
        mid = re.search(r"/p(\d+)", href)
        aid = mid.group(1) if mid else url
    id_annonce = str(aid)

    # Prix : "€ 1,395,000"
    price_el = card.select_one("div.price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Specs : "328 m²  5  6" (surface | salles de bain | chambres)
    specs_el = card.select_one("div.specs")
    specs_text = specs_el.get_text(" ", strip=True) if specs_el else ""
    surface = _parse_surface(specs_text)
    chambres = _parse_chambres(card)

    # Description (souvent vide en liste, chargée en AJAX) — best-effort
    desc_el = card.select_one("[data-role='set-ajax-description']")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Agence
    agence = "LuxuryEstate"
    ag_el = card.select_one(".listed-by .js_clickable")
    if ag_el and ag_el.get_text(strip=True):
        agence = ag_el.get_text(" ", strip=True)
    else:
        ag_img = card.select_one(".agency img")
        if ag_img and ag_img.get("alt"):
            agence = ag_img.get("alt").strip()

    # Photos
    photos = []
    for img in card.select("div.foto img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "luxuryestate",
        "url": url,
        "id_annonce": id_annonce,
        "titre": title_raw[:150],
        "type_bien": type_bien[:60],
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",  # non exposé dans la carte (post-filtre par toponyme)
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": agence[:80],
        "_dept_label": dept_label,  # interne — retiré après le post-filtre
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    """'€ 1,395,000' → 1395000.0 ; ignore les libellés non chiffrés."""
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'328 m² 5 6' → 328.0"""
    m = re.search(r"([\d\s,\.]+)\s*m²", text)
    if not m:
        return None
    val = re.sub(r"[\s,]", "", m.group(1))
    try:
        f = float(val)
        return f if 5 <= f <= 50000 else None
    except ValueError:
        return None


def _parse_chambres(card) -> int | None:
    """Dernier nombre du bloc specs = chambres (icône #bed)."""
    specs = card.select_one("div.specs")
    if not specs:
        return None
    nums = re.findall(r"\b(\d{1,2})\b", specs.get_text(" ", strip=True))
    return int(nums[-1]) if nums else None


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
    print(f"\nTotal LuxuryEstate: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('chambres') or '?'}ch"
            f" — {b['type_bien']} — {b['ville']}"
        )

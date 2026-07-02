"""scrapers/luxuryestate.py — LuxuryEstate.com (agrégateur international de prestige)

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /france/{region-slug}/{dept-slug}?pag={N}
              → filtre département CÔTÉ SERVEUR (une URL par département cible).
Cartes : li.search-list__item  (data-id = id de l'annonce)

Migré sur scrapers/_base.py (modèle le_tuc.py) : HEADERS, boucle département +
pagination, filtres prix/surface et dédup viennent du socle (run_dept_search).
Ne reste ici que le PROPRE au site : slugs region/dept, post-filtre toponyme et
parsing des cartes.

Particularités :
  - Site anglophone (le projet est FR, mais ce portail expose ses libellés en EN).
  - Agrégateur de biens de prestige : prix souvent très élevés → le filtre
    `prix_max` écarte une bonne partie de l'inventaire (comportement attendu).
  - Le code postal n'est PAS exposé dans la carte (seulement Ville + Département
    en clair). Le post-filtre département s'appuie donc sur le TOPONYME : le titre
    se termine par le nom du département (ex. « …, Sarthe », « …, Indre and Loire »),
    re-vérifié contre le nom attendu pour l'URL → 0 fuite hors-zone.
  - Type de bien déduit du préfixe du titre (Luxury home / Villa / Castle…) ;
    les appartements/terrains/commerces sont exclus.

Pagination : ?pag={N} (jusqu'à ~21 pages selon le département, ~14-15 cartes/page).

Interface : async def search(criteres: dict) -> list[dict]
"""

import re
import unicodedata

from scrapers._base import parse_price_digits, run_dept_search, standalone_main

BASE_URL = "https://www.luxuryestate.com"
PHOTOS_PER_CARD = 10

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
    return await run_dept_search(
        source="luxuryestate",
        label="LuxuryEstate",
        page_url=lambda dept, slug, page: (
            f"{BASE_URL}/france/{slug}" + (f"?pag={page}" if page > 1 else "")
        ),
        card_selector="li.search-list__item",
        parse_card=_parse_card,
        criteres=criteres,
        dept_slugs=DEPT_SLUGS,
        max_pages=22,
    )


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

    # Post-filtre département STRICT par toponyme : le titre doit se terminer
    # par le nom du département attendu → 0 fuite hors-zone.
    label = _norm(DEPT_LABELS.get(dept, ""))
    if label and label not in _norm(dept_label):
        return None

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

    # Prix : "€ 1,395,000" (virgules = séparateurs de milliers)
    price_el = card.select_one("div.price")
    prix = parse_price_digits(price_el.get_text(" ", strip=True) if price_el else "")

    # Specs : "328 m²  5  6" (surface | salles de bain | chambres)
    specs_el = card.select_one("div.specs")
    specs_text = specs_el.get_text(" ", strip=True) if specs_el else ""
    surface = _parse_surface(specs_text)
    chambres = _parse_chambres(specs_text)

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
    }


# ── Helpers propres au site ───────────────────────────────────────────────────

def _parse_surface(text: str) -> float | None:
    """'328 m² 5 6' → 328.0 (virgule/point = séparateurs de milliers)."""
    m = re.search(r"([\d\s,\.]+)\s*m²", text)
    if not m:
        return None
    val = re.sub(r"[\s,]", "", m.group(1))
    try:
        f = float(val)
        return f if 5 <= f <= 50000 else None
    except ValueError:
        return None


def _parse_chambres(specs_text: str) -> int | None:
    """Dernier nombre du bloc specs = chambres (icône #bed)."""
    nums = re.findall(r"\b(\d{1,2})\b", specs_text)
    return int(nums[-1]) if nums else None


if __name__ == "__main__":
    standalone_main(search, "LuxuryEstate")

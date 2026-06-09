"""scrapers/beauxvillages.py — Beaux Villages Immobilier (agence anglophone, vieilles pierres)

Méthode : scrape_simple (httpx) — SSR HTML (Joomla / composant OSProperty, site bilingue /en /fr).

Zone couverte : Sud-Ouest + Provence + bande Charente/Limousin
                (Dordogne, Lot, Gers, Gironde, Aude, Var, Charente, Haute-Vienne…).
                AUCUNE implantation dans le grand Val-de-Loire / Ouest : les
                départements 72/28/45/89/49/37… renvoient une 404 (pas de page).
                → sur la zone cible actuelle de criteria.md, ce scraper retourne 0 bien,
                  mais reste fonctionnel (réactiver si la zone évolue vers le SO/Provence).

URL pattern : /en/property-{dept-slug}  → redirige (302) vers la page canonique
              /en/{dept-slug}-property-for-sale  → FILTRE DÉPARTEMENT CÔTÉ SERVEUR
              (vérifié : toutes les cartes d'une page dept appartiennent au même
              département). Un département hors zone → 404 → on skip proprement.

Pagination : liens réels extraits du HTML (a[href*='page-']) — l'URL paginée
             embarque un itemid spécifique par catégorie, donc on ne la construit
             pas à la main, on suit les liens rendus.

Cartes : div.property-card
  - URL    : .swiper-slide[data-url]  ou  a[href] (ex: /en/lot-property-for-sale/bviXXXXX)
  - Loc    : p.list-location  →  "Ville, Département"   (PAS de code postal dans la carte)
  - Détails: .list-details-item (icônes fa-bed / fa-bath / fa-house=surface hab / fa-tree=terrain)
  - Réf    : .list-bottom-ref  →  "BVI85262"  (id_annonce)
  - Prix   : .list-bottom-price  →  "€ 2,499,000"  (format anglais, virgule = milliers)
  - Photos : .swiper-slide img[src]  (media.apimo.pro)

Filtre département : pas de code postal dans les cartes → on filtre sur le NOM de
  département (texte ".list-location" après la virgule) croisé avec le slug de l'URL.
  Le post-filtre STRICT exige que le nom de département de la carte == département ciblé.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://beauxvillages.com"
MAX_PAGES = 10
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9,fr;q=0.8",
}

# Code département → slug utilisé dans /en/property-{slug}.
# Couvre la zone réelle de Beaux Villages (SO / Provence / Charente-Limousin).
# Les départements hors zone (72/28/45/89…) ne sont volontairement PAS listés :
# ils renverraient une 404. On les laisse absents → ils sont simplement ignorés.
DEPT_SLUGS: dict[str, str] = {
    "09": "ariege",
    "11": "aude",
    "12": "aveyron",
    "16": "charente",
    "17": "charente-maritime",
    "19": "correze",
    "23": "creuse",
    "24": "dordogne",
    "30": "gard",
    "31": "haute-garonne",
    "32": "gers",
    "33": "gironde",
    "34": "herault",
    "40": "landes",
    "46": "lot",
    "47": "lot-et-garonne",
    "64": "pyrenees-atlantiques",
    "65": "hautes-pyrenees",
    "66": "pyrenees-orientales",
    "79": "deux-sevres",
    "81": "tarn",
    "82": "tarn-et-garonne",
    "83": "var",
    "84": "vaucluse",
    "86": "vienne",
    "87": "haute-vienne",
    "13": "bouches-du-rhone",
}

# Nom de département (normalisé, tel qu'affiché dans .list-location) → code.
# Sert au post-filtre strict (les cartes n'ont pas de code postal).
DEPT_NAMES: dict[str, str] = {
    "ariege": "09",
    "aude": "11",
    "aveyron": "12",
    "charente": "16",
    "charente maritime": "17",
    "correze": "19",
    "creuse": "23",
    "dordogne": "24",
    "gard": "30",
    "haute garonne": "31",
    "gers": "32",
    "gironde": "33",
    "herault": "34",
    "landes": "40",
    "lot": "46",
    "lot et garonne": "47",
    "pyrenees atlantiques": "64",
    "hautes pyrenees": "65",
    "pyrenees orientales": "66",
    "deux sevres": "79",
    "tarn": "81",
    "tarn et garonne": "82",
    "var": "83",
    "vaucluse": "84",
    "vienne": "86",
    "haute vienne": "87",
    "bouches du rhone": "13",
}

_CP_BY_DEPT: dict[str, str] = {code: code + "000" for code in DEPT_SLUGS}


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
                # Département hors zone Beaux Villages → page inexistante (404). On skip.
                print(f"[BeauxVillages] Dept {dept}: hors zone (pas de page)")
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, slug, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[BeauxVillages] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[BeauxVillages] Erreur dept {dept}: {e}")
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

    url = f"{BASE_URL}/en/property-{slug}"
    visited: set[str] = set()

    for _ in range(MAX_PAGES):
        if url in visited:
            break
        visited.add(url)

        r = await client.get(url)
        if r.status_code != 200:
            break

        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("div.property-card")
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

            # Post-filtre STRICT : le nom de département de la carte doit == dept ciblé.
            if bien["departement"] != dept:
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

        # Lien de page suivante (rendu dans le HTML)
        next_url = _next_page_url(soup, visited)
        if not next_url or new_on_page == 0:
            break
        url = next_url
        await asyncio.sleep(0.5)

    return biens


def _next_page_url(soup, visited: set[str]) -> str | None:
    best_n = None
    best_href = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/page-(\d+)/", href)
        if not m:
            continue
        full = href if href.startswith("http") else BASE_URL + href
        if full in visited:
            continue
        n = int(m.group(1))
        if best_n is None or n < best_n:
            best_n, best_href = n, full
    return best_href


def _parse_card(card, dept: str) -> dict | None:
    # URL de l'annonce
    href = ""
    slide = card.select_one(".swiper-slide[data-url]")
    if slide and slide.get("data-url"):
        href = slide["data-url"]
    if not href:
        a = card.select_one("a[href]")
        href = a.get("href", "") if a else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Localisation : "Ville, Département"
    loc_el = card.select_one(".list-location")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, dept_name = _parse_loc(loc)
    dept_code = DEPT_NAMES.get(dept_name, "")

    # Référence (id_annonce)
    ref_el = card.select_one(".list-bottom-ref")
    ref = ref_el.get_text(strip=True) if ref_el else ""
    if not ref:
        m = re.search(r"/(bvi\d+)", url, re.IGNORECASE)
        ref = m.group(1) if m else url
    id_annonce = ref

    # Prix : "€ 2,499,000" (virgule = séparateur de milliers en anglais)
    price_el = card.select_one(".list-bottom-price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Détails (icônes) : fa-bed / fa-bath / fa-house (surface hab) / fa-tree (terrain)
    chambres = pieces = None
    surface = surface_terrain = None
    for item in card.select(".list-details-item"):
        icon = item.find("i")
        cls = " ".join(icon.get("class", [])) if icon else ""
        val_txt = item.get_text(" ", strip=True)
        num = _first_number(val_txt)
        if "fa-bed" in cls:
            chambres = int(num) if num is not None else chambres
        elif "fa-bath" in cls:
            pass  # salles de bain — non mappé sur le modèle
        elif "fa-house" in cls or "fa-home" in cls:
            surface = num
        elif "fa-tree" in cls:
            surface_terrain = num

    # Type de bien : déduit du segment d'URL (/en/{slug}-property-for-sale/bviXXXX)
    type_bien = "maison"

    # Titre : pas de titre explicite dans la carte → on synthétise
    titre = f"{type_bien.title()} {ville}, {dept_name.title()}".strip(", ").strip()

    # Photos
    photos = []
    for img in card.select(".swiper-slide img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "beauxvillages",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": dept_code,
        "ville": ville[:80],
        "code_postal": _CP_BY_DEPT.get(dept_code, ""),
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Beaux Villages Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Minuscule, sans accents, séparateurs uniformisés en espace."""
    text = text.lower().strip()
    repl = (
        ("à", "a"), ("â", "a"), ("ä", "a"),
        ("é", "e"), ("è", "e"), ("ê", "e"), ("ë", "e"),
        ("î", "i"), ("ï", "i"),
        ("ô", "o"), ("ö", "o"),
        ("ù", "u"), ("û", "u"), ("ü", "u"),
        ("ç", "c"),
    )
    for a, b in repl:
        text = text.replace(a, b)
    text = re.sub(r"[-'’]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_loc(text: str) -> tuple[str, str]:
    """'Montcuq-en-Quercy-Blanc, Lot' → ('Montcuq-en-Quercy-Blanc', 'lot' normalisé)."""
    if "," in text:
        ville, dept = text.rsplit(",", 1)
        return ville.strip(), _normalize(dept)
    return text.strip(), ""


def _parse_price(text: str) -> float | None:
    """'€ 2,499,000' → 2499000.0 (virgule = milliers)."""
    cleaned = re.sub(r"[€£$\s\xa0]", "", text)
    cleaned = cleaned.replace(",", "")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    # Cas d'un point décimal anglais résiduel : on ne garde que la partie entière plausible
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _first_number(text: str) -> float | None:
    """'380,100 m²' → 380100.0 ; '553 m²' → 553.0 ; '8' → 8.0."""
    m = re.search(r"([\d,\.]+)", text)
    if not m:
        return None
    raw = m.group(1)
    # virgule = séparateur de milliers (format anglais)
    raw = raw.replace(",", "")
    try:
        return float(raw)
    except ValueError:
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
    print(f"\nTotal Beaux Villages: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

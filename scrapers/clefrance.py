"""scrapers/clefrance.py — Clé France / Cle France (clefrance.co.uk)

Portail pour acheteurs ANGLOPHONES (réseau Joomla + composant OS Property).
Forte implantation en Mayenne (53), Sarthe (72), Pays-de-la-Loire et Centre.

Méthode : scrape_simple (httpx) — SSR HTML, pas de Cloudflare.

Listing par RÉGION (pas de page sous-département fiable) :
  - Pays de la Loire : /all-properties-for-sale-in-pays-de-la-loire        → depts 53, 72, 49 (+44, 85 hors-cible)
  - Centre           : /all-properties-for-sale-in-centre-france           → depts 28, 45, 41, 37, 36, 18
  (Cle France n'a pas de région Bourgogne → 89 Yonne / 58 Nièvre absents du
   découpage régional ; on tente quand même via post-filtre si présents.)

Pagination (découverte dynamiquement sur la page 1, l'ID de catégorie change
par région) :
  /all-properties-for-sale-in-{slug}/country-and-region-{reg}/page-N/{catid}?property_type=1

Carte : div.property_item
  - data-lat / data-long : coordonnées (présentes sur ~toutes les cartes)
  - a.property_mark_a[href] : URL fiche (le slug contient parfois le NOM du
        département, mais avec des coquilles fréquentes : "mayene", "maine-lore",
        "marneet-loire"… → non fiable seul)
  - h5 > a : titre anglais
  - .property-ref-badge : référence agence (ex: DJV06013)
  - .price : "€ 229,000"
  - .info span (fa-bed / fa-bath) : chambres / salles de bain

FILTRE DÉPARTEMENT — 0 FUITE :
  Le slug d'URL est truffé de coquilles → on n'y fait pas confiance pour le filtre.
  À la place, on REVERSE-GEOCODE les coordonnées de la carte via l'API BAN
  (api-adresse.data.gouv.fr, gratuite) → code_postal + ville officiels → département
  = code_postal[:2]. C'est la source de vérité du filtre (aucun bien hors-cible).
  Le slug ne sert qu'à un pré-filtre grossier pour éviter des appels reverse-geo inutiles.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.clefrance.co.uk"
BAN_REVERSE = "https://api-adresse.data.gouv.fr/reverse/"
MAX_PAGES = 12
PHOTOS_PER_CARD = 1  # 1 photo de couverture sur la liste

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Régions Cle France contenant les départements cibles → (slug listing, segment region)
REGION_LISTINGS = [
    ("pays-de-la-loire", "pays-de-la-loire"),  # 53, 72, 49 (+ 44, 85)
    ("centre-france", "centre"),               # 28, 45, 41, 37, 36, 18
]

# Noms de département (target) pour le pré-filtre grossier sur le slug d'URL.
# Volontairement tolérant (les coquilles du site sont gérées par le reverse-geo).
_DEPT_NAME_HINTS = [
    "sarthe", "mayenne", "mayene", "maine-et-loire", "maine-loire", "maine-lore",
    "eure-et-loir", "eure-et-loire", "loiret", "loir-et-cher", "loir-cher",
    "indre-et-loire", "indre", "cher", "yonne", "nievre", "centre",
    "pays-de-la-loire", "pays-maine", "marneet-loire", "blank", "loire",
]

# Exclure plans/terrains/commerces + biens VENDUS + services Club Cle France
_EXCLUDE = re.compile(
    r"\bbuilding plot\b|\bunserviced\b|\bland for sale\b|\bplot for sale\b|"
    r"\bgarage\b|\bcommercial\b|\bbusiness premises\b|\bapartment\b|"
    r"sold by cle france|sold by clé france|groundworks|septic tank|"
    r"property renovation|property surveys|gite accommodation",
    re.IGNORECASE,
)
# Préfixes de référence non vendables (SLD = Sold)
_SOLD_REF = re.compile(r"^SLD", re.IGNORECASE)

# Type de bien depuis le titre anglais
_TYPE_MAP = [
    (re.compile(r"ch[aâ]teau", re.IGNORECASE), "château"),
    (re.compile(r"manor|manoir", re.IGNORECASE), "manoir"),
    (re.compile(r"longere|longère|farmhouse|farm house", re.IGNORECASE), "longère"),
    (re.compile(r"mill|moulin", re.IGNORECASE), "moulin"),
    (re.compile(r"villa", re.IGNORECASE), "villa"),
    (re.compile(r"\bgite\b|g[iî]te", re.IGNORECASE), "gîte"),
    (re.compile(r"property|propriete|propriété|estate", re.IGNORECASE), "propriété"),
    (re.compile(r"house|maison|cottage|home", re.IGNORECASE), "maison"),
]


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    if not departements:
        return []

    results: list[dict] = []
    seen: set[str] = set()
    geo_cache: dict[tuple, tuple] = {}

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
    ) as client:
        for slug, reg in REGION_LISTINGS:
            try:
                cards = await _fetch_region_cards(client, slug, reg)
            except Exception as e:
                print(f"[CleFrance] Erreur région {slug}: {e}")
                continue

            kept = 0
            for card in cards:
                bien = _parse_card(card)
                if not bien:
                    continue

                # Pré-filtre grossier : si le slug nomme une région/dept et qu'aucun
                # indice cible n'y figure, on saute (évite des reverse-geo inutiles).
                # (on garde quand même les slugs "town-only" qui n'ont aucun indice)
                # -> géré directement par le reverse-geo ci-dessous.

                # Filtre département FIABLE via reverse-geo des coordonnées
                lat, lon = bien.pop("_lat", None), bien.pop("_lon", None)
                cp, ville_off = await _reverse_dept(client, lat, lon, geo_cache)
                if not cp:
                    continue
                dept = cp[:2]
                if dept not in departements:
                    continue

                bien["code_postal"] = cp
                bien["departement"] = dept
                if ville_off:
                    bien["ville"] = ville_off

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if not p:
                    continue  # services / prix manquant → pas un bien vendable
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                aid = bien.get("id_annonce") or bien.get("url")
                if aid in seen:
                    continue
                seen.add(aid)
                results.append(bien)
                kept += 1

            print(f"[CleFrance] Région {slug}: {len(cards)} cartes → {kept} retenues (depts cibles)")

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[CleFrance] Dept {dept}: {n} annonces")

    return results


async def _fetch_region_cards(client: httpx.AsyncClient, slug: str, reg: str) -> list:
    """Récupère toutes les cartes property_item d'une région (toutes pages)."""
    cards: list = []
    catid: str | None = None

    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = f"{BASE_URL}/all-properties-for-sale-in-{slug}"
        else:
            if not catid:
                break
            url = (
                f"{BASE_URL}/all-properties-for-sale-in-{slug}"
                f"/country-and-region-{reg}/page-{page}/{catid}?property_type=1"
            )

        r = await client.get(url)
        if r.status_code != 200:
            break

        soup = BeautifulSoup(r.text, "html.parser")
        page_cards = soup.select("div.property_item")
        if not page_cards:
            break
        cards.extend(page_cards)

        if page == 1:
            # Découvre l'ID de catégorie depuis un lien de pagination
            link = soup.select_one('a[href*="/page-2/"]')
            if link:
                m = re.search(r"/page-2/(\d+)", link.get("href", ""))
                if m:
                    catid = m.group(1)
            if not catid:
                break  # pas de pagination → une seule page

        # Dernière page si plus de lien "page suivante"
        if not soup.select_one(f'a[href*="/page-{page + 1}/"]'):
            break

        await asyncio.sleep(0.4)

    return cards


def _parse_card(card) -> dict | None:
    a = card.select_one("a.property_mark_a[href]")
    if not a:
        return None
    href = a.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Coordonnées (source du filtre département)
    lat = _to_float(card.get("data-lat"))
    lon = _to_float(card.get("data-long"))
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return None  # sans coords on ne peut pas filtrer de façon fiable

    # Titre
    h5 = card.select_one("h5 a, h5")
    titre = h5.get_text(" ", strip=True) if h5 else ""
    if not titre:
        img = card.select_one("img[title]")
        titre = img.get("title", "").strip() if img else ""

    blob = f"{titre} {href}"
    if _EXCLUDE.search(blob):
        return None

    # Référence agence
    ref_el = card.select_one(".property-ref-badge")
    ref = ""
    if ref_el:
        ref = re.sub(r"[^A-Za-z0-9]", "", ref_el.get_text(strip=True))
    if ref and _SOLD_REF.match(ref):
        return None  # bien vendu
    # id depuis l'image (id="picture_0") indispo → utilise data-property-id
    fav = card.select_one("[data-property-id]")
    pid = fav.get("data-property-id") if fav else None
    id_annonce = ref or pid or url

    # Prix : "€ 229,000"
    price_el = card.select_one(".price")
    prix = _parse_price(price_el.get_text(" ", strip=True)) if price_el else None

    # Chambres / salles de bain : .info span (icônes fa-bed / fa-bath)
    chambres = None
    info = card.select_one(".property-details .info, .info")
    if info:
        spans = info.find_all("span")
        for sp in spans:
            icon = sp.find("i")
            cls = " ".join(icon.get("class", [])) if icon else ""
            num = _to_int(sp.get_text(" ", strip=True))
            if num is None:
                continue
            if "bed" in cls and chambres is None:
                chambres = num

    type_bien = "maison"
    for rx, label in _TYPE_MAP:
        if rx.search(titre):
            type_bien = label
            break

    # Photo de couverture
    photos: list[str] = []
    img = card.select_one("img.oslazy, img[data-original], figure img")
    if img:
        src = img.get("data-original") or img.get("src") or ""
        if src.startswith("http"):
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    if not titre:
        titre = "Propriété Cle France"

    return {
        "source": "clefrance",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": None,
        "departement": "",        # rempli par reverse-geo
        "ville": "",              # rempli par reverse-geo (officiel)
        "code_postal": "",        # rempli par reverse-geo
        "surface": None,          # non exposée sur la liste
        "surface_terrain": None,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "dpe": None,
        "photos": photos,
        "agence": "Cle France",
        "latitude": lat,
        "longitude": lon,
        "geo_precis": True,
        "_lat": lat,
        "_lon": lon,
    }


async def _reverse_dept(
    client: httpx.AsyncClient,
    lat: float | None,
    lon: float | None,
    cache: dict,
) -> tuple[str | None, str | None]:
    """Reverse-geocode via API BAN → (code_postal, ville). Source du filtre dept."""
    if lat is None or lon is None:
        return None, None
    key = (round(lat, 4), round(lon, 4))
    if key in cache:
        return cache[key]

    cp = ville = None
    try:
        r = await client.get(
            BAN_REVERSE,
            params={"lat": f"{lat}", "lon": f"{lon}"},
            headers={"Accept": "application/json"},
        )
        if r.status_code == 200:
            feats = r.json().get("features") or []
            if feats:
                props = feats[0].get("properties", {})
                cp = props.get("postcode")
                ville = props.get("city")
    except Exception:
        cp = ville = None

    cache[key] = (cp, ville)
    await asyncio.sleep(0.05)
    return cp, ville


# ── Helpers ──────────────────────────────────────────────────────────────────

def _to_float(text) -> float | None:
    if text is None:
        return None
    try:
        return float(str(text).strip())
    except (ValueError, TypeError):
        return None


def _to_int(text) -> int | None:
    m = re.search(r"\d+", str(text))
    return int(m.group(0)) if m else None


def _parse_price(text: str) -> float | None:
    """'€ 229,000' → 229000.0"""
    cleaned = re.sub(r"[^\d]", "", str(text))
    try:
        return float(cleaned) if cleaned else None
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
    print(f"\nTotal Cle France (depts cibles): {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('chambres') or '?'} ch"
            f" — {b['type_bien']} — {b['ville']}"
        )

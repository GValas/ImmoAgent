"""scrapers/francepropertyshop.py — France Property Shop (portail anglophone)

Méthode : scrape_simple (httpx) — SSR HTML (Cloudflare CDN mais PAS de challenge :
          le HTML brut contient déjà toutes les annonces).
URL pattern : /french-property-for-sale/{region-slug}/{dept-slug}/?page=N
              → filtre département CÔTÉ SERVEUR via le slug de département.
              (ex: /french-property-for-sale/pays-de-la-loire/mayenne/?page=2)

Particularité : aucun code postal n'est exposé (ni en liste, ni en page détail —
                seulement la commune + la région). Le post-filtre département se fait
                donc en résolvant la COMMUNE → code département via geo.api.gouv.fr
                (cache mémoire, match sur le nom exact). Couplé au slug serveur, cela
                garantit 0 fuite hors-zone.

Cartes : div.card  (data-fake-anchor="/listing/{id}")
  - URL/id : data-fake-anchor ou .card__name a[href]  → /listing/{id}
  - Commune: .card__flash
  - Titre  : .card__name a
  - Prix   : .card__price            → "€172,800"
  - Icônes : .card__icons → img[src*=item_bedrooms_icon] (chambres, fiable),
             img[src*=item_area_icon]   (surface m²),
             img[src*=item_rooms_icon]  (NON fiable : recopie souvent la surface →
                                          ignoré pour `pieces`).
  - Photo  : .card__gallery[data-bgsrc] (image principale)

Type de bien : non structuré → déduit du titre (house/cottage/longère/manor/farm/
               château/mill…), défaut "maison". Biens anglophones de campagne.

Couverture : portail national couvrant toute la France par dept-slug. Sur la zone
             cible, la Mayenne (53) et les départements Loire/Anjou ont du stock réel.
             Slugs de département mappés pour les 11 départements cibles.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.francepropertyshop.com"
MAX_PAGES = 8
PHOTOS_PER_CARD = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Code département → (region-slug, dept-slug) tels qu'utilisés dans l'URL serveur.
DEPT_SLUGS: dict[str, tuple[str, str]] = {
    "72": ("pays-de-la-loire", "sarthe"),
    "28": ("centre-val-de-loire", "eure-et-loir"),
    "45": ("centre-val-de-loire", "loiret"),
    "89": ("bourgogne-franche-comte", "yonne"),
    "49": ("pays-de-la-loire", "maine-et-loire"),
    "37": ("centre-val-de-loire", "indre-et-loire"),
    "36": ("centre-val-de-loire", "indre"),
    "18": ("centre-val-de-loire", "cher"),
    "58": ("bourgogne-franche-comte", "nievre"),
    "41": ("centre-val-de-loire", "loir-et-cher"),
    "53": ("pays-de-la-loire", "mayenne"),
}

# Type de bien déduit du titre anglophone (on ne garde que maisons / propriétés).
_KEEP_TYPE = re.compile(
    r"house|home|cottage|longere|longère|manor|manoir|farm|farmhouse|propert|"
    r"chateau|château|mill|moulin|barn|maison|villa|mansion|estate|stone",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"apartment|appartement|flat|land\b|building plot|commercial|garage|"
    r"office|shop\b|business",
    re.IGNORECASE,
)

# Cache mémoire commune(normalisée) → code département (geo.api.gouv.fr)
_DEPT_CACHE: dict[str, str | None] = {}


def _norm_commune(nom: str) -> str:
    return re.sub(r"\s+", " ", nom or "").strip()


async def _commune_departement(
    client: httpx.AsyncClient, commune: str
) -> str | None:
    """Résout commune → code département via geo.api.gouv.fr (match nom exact).

    Renvoie None si introuvable/ambigu. Mis en cache pour éviter les requêtes
    répétées. Sert de garde-fou strict : couplé au slug serveur, 0 fuite.
    """
    key = _norm_commune(commune)
    if not key:
        return None
    if key in _DEPT_CACHE:
        return _DEPT_CACHE[key]
    dept: str | None = None
    try:
        r = await client.get(
            "https://geo.api.gouv.fr/communes",
            params={
                "nom": key,
                "fields": "nom,codeDepartement",
                "boost": "population",
                "limit": 10,
            },
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            key_low = key.casefold()
            # 1) match sur le nom exact (lève l'ambiguïté des homonymes)
            for c in data:
                if (c.get("nom") or "").casefold() == key_low:
                    dept = c.get("codeDepartement")
                    break
            # 2) sinon, si un seul résultat, on le prend
            if dept is None and len(data) == 1:
                dept = data[0].get("codeDepartement")
    except Exception:
        dept = None
    _DEPT_CACHE[key] = dept
    return dept


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
    ) as client:
        for dept in departements:
            slugs = DEPT_SLUGS.get(dept)
            if not slugs:
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, slugs, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[FrancePropertyShop] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[FrancePropertyShop] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    slugs: tuple[str, str],
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    region_slug, dept_slug = slugs
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = (
            f"{BASE_URL}/french-property-for-sale/"
            f"{region_slug}/{dept_slug}/?page={page}"
        )
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select("div.card")
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            try:
                bien = await _parse_card(client, card, dept)
            except Exception:
                continue
            if not bien:
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

        if new_on_page == 0:
            break

        await asyncio.sleep(0.5)

    return biens


async def _parse_card(client: httpx.AsyncClient, card, dept: str) -> dict | None:
    # URL + id
    href = card.get("data-fake-anchor", "")
    if not href:
        link = card.select_one(".card__name a[href]")
        href = link.get("href", "") if link else ""
    if not href:
        return None
    m_id = re.search(r"/listing/(\d+)", href)
    id_annonce = m_id.group(1) if m_id else href
    url = href if href.startswith("http") else BASE_URL + href

    # Commune
    flash = card.select_one(".card__flash")
    ville = flash.get_text(" ", strip=True) if flash else ""
    if not ville:
        return None

    # Garde-fou STRICT : commune → département (doit == dept cible)
    dept_resolu = await _commune_departement(client, ville)
    if dept_resolu is not None and dept_resolu != dept:
        return None  # fuite hors-zone : rejet
    # Si non résolu (None), le slug serveur garantit déjà le département → on garde.

    # Titre
    name_el = card.select_one(".card__name a") or card.select_one(".card__name")
    titre = name_el.get_text(" ", strip=True) if name_el else ""

    # Type de bien (déduit du titre)
    type_bien = "maison"
    if _EXCLUDE_TYPE.search(titre) and not _KEEP_TYPE.search(titre):
        return None
    if re.search(r"cottage", titre, re.I):
        type_bien = "maison"
    elif re.search(r"manor|manoir", titre, re.I):
        type_bien = "manoir"
    elif re.search(r"longere|longère", titre, re.I):
        type_bien = "longere"
    elif re.search(r"chateau|château", titre, re.I):
        type_bien = "propriete"
    elif re.search(r"mill|moulin|farm|estate|propert", titre, re.I):
        type_bien = "propriete"

    # Prix : "€172,800"
    price_el = card.select_one(".card__price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Icônes : chambres (fiable) + surface ; rooms ignoré (non fiable)
    icons = card.select_one(".card__icons")
    chambres = None
    surface = None
    if icons:
        for img in icons.find_all("img"):
            src = img.get("src", "")
            nxt = img.next_sibling
            txt = str(nxt).strip() if nxt else ""
            num = re.search(r"\d[\d\s]*", txt)
            val = int(re.sub(r"\s", "", num.group(0))) if num else None
            if "item_bedrooms_icon" in src and val:
                chambres = val
            elif "item_area_icon" in src and val:
                surface = float(val)

    # Photo principale (data-bgsrc de la galerie)
    photos: list[str] = []
    gal = card.select_one(".card__gallery")
    if gal:
        bg = gal.get("data-bgsrc") or gal.get("data-src") or ""
        if bg and not bg.startswith("data:"):
            if bg.startswith("//"):
                bg = "https:" + bg
            photos.append(bg)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "francepropertyshop",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",  # non exposé par le site
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,  # rooms icon non fiable → laissé vide
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "France Property Shop",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    """'€172,800' / '172 800 €' → 172800.0 (les virgules sont des séparateurs
    de milliers anglophones)."""
    cleaned = re.sub(r"[^\d]", "", text)
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
    print(f"\nTotal France Property Shop: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('chambres') or '?'}ch"
            f" — {b['type_bien']} — {b['ville']}"
        )

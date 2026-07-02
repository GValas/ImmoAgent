"""scrapers/demeuresenperigord.py — Demeures en Périgord (agence Dordogne / Périgord Noir)

Méthode : scrape_simple (httpx) — SSR HTML (logiciel Label-Pierres / jriou.fr).

Périmètre RÉEL du site : agence mono-département implantée aux Eyzies (Dordogne,
24, Nouvelle-Aquitaine), spécialisée maisons de caractère, châteaux et propriétés
de campagne (~431 offres). L'inventaire est intégralement en Dordogne (24) et,
marginalement, dans les départements limitrophes (Lot 46, Corrèze 19…). AUCUN bien
dans la zone cible actuelle (72/28/45/89) → ce scraper renvoie normalement 0 bien
sur ces départements. Code conservé/fonctionnel ; à réactiver si la zone cible
inclut un jour la Dordogne.

URL liste : /nos-biens-immobiliers/?ref=&dept=&region=&secteur=&typ=&prixmin=&prixmax=&page_number=N
  ⚠️ Le paramètre serveur `dept` est NON FONCTIONNEL : quelle que soit sa valeur
     (24, 72, 45…), la même liste complète est renvoyée. On ne peut donc PAS se
     fier au filtre serveur. → Filtre département STRICT en post-traitement.

Cartes (24/page, 18 pages) : li.properties_item
  - URL    : a[href*="/detail/"]   → /detail/{slug}-{id}.html
  - Réf    : .preview_subtitle .caps  (ex : "DEP1021")
  - Titre  : h3.preview_title
  - Métas  : .favorites_cell_meta (ordre : pièces | "NN m²" surface | "NN m²" terrain)
  - Prix   : .preview_price        → "140 400 € HAI"
  - Photo  : .preview_img img[src]
  Les cartes n'exposent NI ville NI code postal → on lit la page détail.

Page détail :
  - Secteur/ville : élément #m-eac-city-cp / texte "Secteur : Région {VILLE}"
  - Type : H1 + "Vente {Type} Région {VILLE}"
  Aucun code postal propre au bien dans le HTML (seul le CP de l'agence apparaît
  en pied de page). → On géocode le nom de ville via l'API BAN officielle
  (api-adresse.data.gouv.fr, gratuite) pour obtenir CP + département, puis on
  applique le filtre STRICT code_postal[:2] ∈ départements cibles. 0 fuite garanti.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.demeuresenperigord.fr"
LIST_PATH = "/nos-biens-immobiliers/"
BAN_URL = "https://api-adresse.data.gouv.fr/search/"
MAX_PAGES = 20
PHOTOS_PER_CARD = 1  # la liste n'expose que la photo de couverture


# Type de bien (segment <select name="typ">) → on ne garde que propriétés/maisons.
_TYP_KEEP = {"mai"}  # "Propriétés" (maisons / demeures / châteaux)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)

# Petit cache de géocodage ville → (cp, dept) pour éviter les requêtes répétées.
_GEO_CACHE: dict[str, tuple[str | None, str | None]] = {}


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = (
                f"{BASE_URL}{LIST_PATH}?ref=&dept=&region=&secteur=&typ="
                f"&prixmin=&prixmax=&page_number={page}"
            )
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[DemeuresPerigord] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("li.properties_item")
            if not cards:
                break

            for card in cards:
                try:
                    bien = await _parse_card(
                        client, card, departements, prix_max, prix_min, surface_min
                    )
                except Exception:
                    continue
                if not bien:
                    continue
                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                results.append(bien)

            await asyncio.sleep(0.6)

    print(f"[DemeuresPerigord] {len(results)} bien(s) retenu(s) dans la zone cible")
    return results


async def _parse_card(
    client: httpx.AsyncClient,
    card,
    departements: list[str],
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> dict | None:
    link = card.select_one('a[href*="/detail/"]')
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce : id numérique en fin de slug, complété par la référence courte.
    id_num = ""
    m = re.search(r"-(\d+)\.html$", href)
    if m:
        id_num = m.group(1)
    ref_el = card.select_one(".preview_subtitle .caps")
    ref = ref_el.get_text(strip=True) if ref_el else ""
    id_annonce = id_num or ref or url

    # Titre
    title_el = card.select_one("h3.preview_title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    # Type de bien : déduit du titre/slug (pas de champ dédié dans la carte).
    type_bien = _type_from_text(f"{titre} {href}")
    if type_bien is None:
        return None

    # Métas : pièces | surface | terrain (ordre observé, libellés absents).
    pieces = chambres = None
    surface = surface_terrain = None
    metas = [m.get_text(" ", strip=True) for m in card.select(".favorites_cell_meta")]
    surfaces_m2: list[float] = []
    for meta in metas:
        mm = re.search(r"([\d\s\xa0]+)\s*m", meta)
        if mm:
            val = _to_float(mm.group(1))
            if val:
                surfaces_m2.append(val)
        elif re.fullmatch(r"\d+", meta.strip()):
            pieces = int(meta.strip())
    if surfaces_m2:
        surface = surfaces_m2[0]
        if len(surfaces_m2) > 1:
            surface_terrain = max(surfaces_m2[1:])

    # Prix
    price_el = card.select_one(".preview_price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Photo de couverture
    photos: list[str] = []
    img = card.select_one(".preview_img img") or card.select_one("img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    # Bornes prix / surface (sans exclure un champ manquant).
    if prix_max and prix and prix > prix_max:
        return None
    if prix_min and prix and prix < prix_min:
        return None
    if surface_min and surface and surface < surface_min:
        return None

    # Localisation : seule la page détail porte la ville (secteur). Pas de CP
    # propre au bien → géocodage BAN pour obtenir CP + département.
    ville, description, dpe = await _fetch_detail(client, url)
    code_postal, dept = await _geocode(client, ville)

    # Filtre département STRICT (le filtre serveur `dept` est inopérant).
    if not dept or dept not in departements:
        return None
    if code_postal and code_postal[:2] != dept:
        return None

    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    return {
        "source": "demeuresenperigord",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": (description or "")[:1200],
        "departement": dept,
        "ville": (ville or "")[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Demeures en Périgord",
    }


async def _fetch_detail(
    client: httpx.AsyncClient, url: str
) -> tuple[str | None, str | None, str | None]:
    """Renvoie (ville/secteur, description, dpe) depuis la page détail."""
    try:
        r = await client.get(url)
    except Exception:
        return None, None, None
    if r.status_code != 200:
        return None, None, None
    soup = BeautifulSoup(r.text, "html.parser")

    ville = None
    # "Secteur : Région {VILLE}" ou "Vente ... Région {VILLE}"
    m = re.search(r"R[ée]gion\s+([A-ZÀ-Ÿ][A-ZÀ-Ÿ '\-]{2,40})", r.text)
    if m:
        ville = m.group(1).strip().title()

    desc_el = soup.select_one(".eac-description, #m-eac-description, .description")
    description = desc_el.get_text(" ", strip=True) if desc_el else None
    if not description:
        h1 = soup.find("h1")
        description = h1.get_text(" ", strip=True) if h1 else None

    dpe = None
    mdpe = re.search(r"\bDPE\b[^A-G]{0,20}\b([A-G])\b", r.text)
    if mdpe:
        dpe = mdpe.group(1)

    return ville, description, dpe


async def _geocode(
    client: httpx.AsyncClient, ville: str | None
) -> tuple[str | None, str | None]:
    """Ville → (code_postal, departement) via l'API BAN officielle (gratuite)."""
    if not ville:
        return None, None
    key = ville.lower().strip()
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]
    cp = dept = None
    try:
        r = await client.get(
            BAN_URL,
            params={"q": ville, "type": "municipality", "limit": 1},
        )
        if r.status_code == 200:
            feats = r.json().get("features") or []
            if feats:
                props = feats[0].get("properties", {})
                cp = props.get("postcode")
                ctx = props.get("context", "")  # "24, Dordogne, ..."
                mctx = re.match(r"\s*(\d{2,3})", ctx)
                if mctx:
                    dept = mctx.group(1).zfill(2)[:2]
                elif cp:
                    dept = cp[:2]
    except Exception:
        pass
    _GEO_CACHE[key] = (cp, dept)
    return cp, dept


# ── Helpers ──────────────────────────────────────────────────────────────────

def _type_from_text(text: str) -> str | None:
    t = text.lower()
    if _EXCLUDE_TYPE.search(t):
        return None
    for kw, label in (
        ("château", "château"),
        ("chateau", "château"),
        ("manoir", "manoir"),
        ("demeure", "demeure"),
        ("propriété", "propriété"),
        ("propriete", "propriété"),
        ("domaine", "domaine"),
        ("moulin", "moulin"),
        ("ferme", "ferme"),
        ("longère", "longère"),
        ("longere", "longère"),
        ("maison", "maison"),
        ("villa", "villa"),
    ):
        if kw in t:
            return label
    # Site de propriétés de caractère : par défaut on considère une propriété.
    return "propriété"


def _to_float(text: str) -> float | None:
    cleaned = re.sub(r"[\s\xa0]", "", text)
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
    print(f"\nTotal Demeures en Périgord: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

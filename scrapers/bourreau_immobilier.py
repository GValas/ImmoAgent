"""scrapers/bourreau_immobilier.py — Bourreau Immobilier (agence indépendante, Joigny 89)

Méthode : scrape_simple (httpx) — SSR HTML (CMS "Elone")
Site mono-département : agence basée à Joigny (Yonne 89), inventaire concentré
sur l'Yonne et ses communes limitrophes. URL pattern de la liste :
    /fr/ventes?page=N        (N = 1..8, ~12 biens/page, ~95 biens au total)

Cartes : ul.listing > li.property[data-property-id]
  - URL    : a[href^="/fr/propriete/"]   → /fr/propriete/{slug}+{id}
  - Type+Ville : h3            → "Pavillon, Brion"   (type, ville)
  - Secteur    : h2            → "15MN DE JOIGNY - MIGENNES" (titre commercial)
  - Prix       : li.price      → "241 000 €"
  - Pièces     : li contenant span.rooms     → "6 pièces"
  - Chambres   : li contenant span.bedrooms  → "4 chambres"
  - Surface    : li contenant span.area      → "150 m²"
  - Photo      : figure img[src]

Filtre DÉPARTEMENT — la carte ET la page détail n'exposent PAS le code postal
du bien (le seul CP de la page détail est celui de l'agence, 89300). On résout
donc la VILLE de la carte en code postal via l'API officielle geo.api.gouv.fr,
en EXIGEANT une correspondance de nom EXACTE dans l'un des départements cibles.
Tout bien dont la ville ne se résout pas à un département cible est ÉCARTÉ
(stratégie conservatrice → 0 fuite garantie, y compris aux frontières 77/45/58/21).

Type de bien : on garde maison / pavillon / propriété / fermette / longère /
manoir / hôtel particulier / domaine / château ; on exclut appartement /
immeuble / terrain / local / garage / fonds de commerce.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import unicodedata

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://bourreau-immobilier.com"
MAX_PAGES = 10
PHOTOS_PER_CARD = 1  # la liste n'expose qu'une vignette ; gallery.py enrichira

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

GEO_API = "https://geo.api.gouv.fr/communes"

# Types de bien (1er mot du h3) à conserver
_KEEP_TYPE = re.compile(
    r"maison|pavillon|propriete|propriété|villa|ferme|fermette|longere|longère|"
    r"manoir|chateau|château|moulin|demeure|domaine|mas|gite|gîte|hotel|hôtel|"
    r"corps de ferme",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"viager",
    re.IGNORECASE,
)


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def _norm_ville(s: str) -> str:
    """Normalise un nom de commune pour comparaison (sans accents, minuscules,
    tirets/espaces/apostrophes unifiés)."""
    s = _strip_accents(s).lower().strip()
    s = re.sub(r"['’]", " ", s)
    s = re.sub(r"[\s\-]+", " ", s)
    return s.strip()


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    dept_set = set(departements)
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    geo_cache: dict[str, str | None] = {}

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        seen_ids: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/fr/ventes?page={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Bourreau] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("li.property")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                try:
                    bien = await _parse_card(card, client, dept_set, geo_cache)
                except Exception:
                    continue
                if not bien:
                    continue

                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue

                # Filtre département STRICT (code postal résolu via geo.api.gouv.fr)
                if not bien["code_postal"] or bien["code_postal"][:2] not in dept_set:
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
                new_on_page += 1

            print(f"[Bourreau] Page {page}: {len(cards)} cartes, {new_on_page} retenues")
            if new_on_page == 0 and len(cards) < 12:
                # dernière page partielle sans bien retenu → fin probable
                pass
            await asyncio.sleep(0.5)

    print(f"[Bourreau] Total retenu (départements cibles) : {len(results)}")
    return results


async def _resolve_cp(
    ville: str,
    dept_set: set[str],
    client: httpx.AsyncClient,
    cache: dict[str, str | None],
) -> str | None:
    """Résout une commune en code postal d'un département cible.
    EXIGE une correspondance de nom exacte dans un dept cible → None sinon."""
    key = _norm_ville(ville)
    if not key:
        return None
    if key in cache:
        return cache[key]

    cp: str | None = None
    try:
        r = await client.get(
            GEO_API,
            params={
                "nom": ville,
                "fields": "nom,codesPostaux,codeDepartement",
                "boost": "population",
                "limit": 10,
            },
        )
        if r.status_code == 200:
            for c in r.json():
                if c.get("codeDepartement") not in dept_set:
                    continue
                if _norm_ville(c.get("nom", "")) != key:
                    continue
                cps = c.get("codesPostaux") or []
                if cps:
                    cp = cps[0]
                    break
    except Exception:
        cp = None

    cache[key] = cp
    return cp


async def _parse_card(
    card,
    client: httpx.AsyncClient,
    dept_set: set[str],
    geo_cache: dict[str, str | None],
) -> dict | None:
    link = card.select_one('a[href^="/fr/propriete/"]')
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    pid = card.get("data-property-id", "")
    m_id = re.search(r"\+(\d+)$", href)
    id_annonce = pid or (m_id.group(1) if m_id else url)

    # h3 = "Type, Ville"
    h3 = card.select_one("h3")
    h3_txt = h3.get_text(" ", strip=True) if h3 else ""
    type_part, _, ville_part = h3_txt.partition(",")
    type_bien = type_part.strip().lower() or "maison"
    ville = ville_part.strip()

    # Filtre type
    if _EXCLUDE_TYPE.search(type_bien) and not _KEEP_TYPE.search(type_bien):
        return None
    if not _KEEP_TYPE.search(type_bien):
        return None

    if not ville:
        return None

    # Code postal via geocodage strict (départements cibles uniquement)
    code_postal = await _resolve_cp(ville, dept_set, client, geo_cache)
    dept = code_postal[:2] if code_postal else ""

    # Titre commercial (h2) ; secours = h3
    h2 = card.select_one("h2")
    secteur = h2.get_text(" ", strip=True) if h2 else ""
    titre = (f"{type_bien.title()} {ville} — {secteur}".strip(" —")
             if secteur else f"{type_bien.title()} {ville}")

    # Prix
    price_el = card.select_one("li.price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Pièces / chambres / surface : repérés par leur span sibling
    pieces = _li_value(card, "rooms")
    chambres = _li_value(card, "bedrooms")
    surface = _li_value_float(card, "area")

    # Photo (vignette unique)
    photos = []
    img = card.select_one("figure img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "bourreau_immobilier",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": secteur[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal or "",
        "surface": surface,
        "surface_terrain": None,  # absent de la liste ; gallery.py / texte détail l'extrairont
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Bourreau Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _li_value(card, span_class: str) -> int | None:
    """Le <li> contient <span class="rooms"></span>6 pièces → 6."""
    span = card.select_one(f"li span.{span_class}")
    if not span:
        return None
    li = span.find_parent("li")
    if not li:
        return None
    m = re.search(r"(\d+)", li.get_text(" ", strip=True))
    return int(m.group(1)) if m else None


def _li_value_float(card, span_class: str) -> float | None:
    span = card.select_one(f"li span.{span_class}")
    if not span:
        return None
    li = span.find_parent("li")
    if not li:
        return None
    txt = li.get_text(" ", strip=True)
    m = re.search(r"([\d\s\xa0]+)\s*m", txt)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
        return f if 8 <= f <= 5000 else None
    except ValueError:
        return None


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
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
    print(f"\nTotal Bourreau Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p/{b.get('chambres') or '?'}ch"
            f" — {b['type_bien']} — {b['ville']}"
        )

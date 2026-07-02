"""scrapers/sud_loire_immobilier.py — Sud Loire Immobilier (AFFTERIMMO, Blois 41)

Méthode : scrape_simple (httpx) — SSR HTML (CMS Periimmo/AC3).
Agence indépendante de Blois (préfecture du Loir-et-Cher), couvre le sud-Loire
autour de Blois : Vineuil, La Chaussée-Saint-Victor, Averdon, Cellettes,
Le Controis-en-Sologne, Montrichard… → quasi-exclusivement Loir-et-Cher (41),
avec quelques communes limitrophes Indre-et-Loire (37).

URL liste  : /annonces/transaction/Vente.html              (page 1)
             /annonces/transaction_____{N}/vente.html       (page N ≥ 2)
             ~11 pages, 9 cartes/page.

Cartes : div.product
  - URL/titre : a.product-image[href] + .product-name
                (.product-name = "<titre> , <Ville>" → la dernière puce = ville)
  - Prix      : .product-price                         → "247 800 €"
  - Pièces    : .data-list__item--NbPiece .data-list__item--value
  - Surface   : .data-list__item--Surface .data-list__item--value
  - Réf       : .data-list__item--products_model .data-list__item--value
  - Photos    : a.product-image img.photo[src] (+ .photo-hidden)

Filtre département : la carte n'expose QUE le nom de ville (pas le code postal ;
le seul CP du HTML détail est celui de l'agence). On résout donc chaque ville en
code postal via l'API BAN officielle (api-adresse.data.gouv.fr, type=municipality,
citycode → préfixe département), avec cache mémoire. Post-filtre STRICT :
on ne garde que les biens dont le département résolu ∈ départements cibles.
→ 0 fuite hors-zone vérifiée.

Type de bien : déduit du segment de slug d'URL (4-40-26 = maison, 3-33-27 =
appartement…) + du titre. On ne garde que maisons / propriétés / longères.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.sudloireimmobilier.fr"
LIST_URL_P1 = f"{BASE_URL}/annonces/transaction/Vente.html"
LIST_URL_PN = f"{BASE_URL}/annonces/transaction_____{{page}}/vente.html"
MAX_PAGES = 12
PHOTOS_PER_CARD = 10

BAN_URL = "https://api-adresse.data.gouv.fr/search/"


# Types de bien (titre / slug) à conserver : maisons / propriétés / longères…
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|pavillon|corps de ferme|gite|gîte|bastide",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|studio|type 1|t1\b",
    re.IGNORECASE,
)

# Cache mémoire : ville (normalisée) -> code_postal résolu (str | None)
_CP_CACHE: dict[str, str | None] = {}


async def _ville_to_cp(client: httpx.AsyncClient, ville: str) -> str | None:
    """Résout un nom de commune en code postal via l'API BAN (cache mémoire)."""
    key = ville.strip().lower()
    if not key:
        return None
    if key in _CP_CACHE:
        return _CP_CACHE[key]
    cp: str | None = None
    try:
        r = await client.get(
            BAN_URL,
            params={"q": ville, "type": "municipality", "limit": 1},
            timeout=15,
        )
        if r.status_code == 200:
            feats = r.json().get("features", [])
            if feats:
                props = feats[0].get("properties", {})
                # citycode (INSEE) = préfixe dept fiable ; postcode pour l'affichage
                cp = props.get("postcode") or None
                citycode = props.get("citycode") or ""
                if cp and citycode[:2] != cp[:2]:
                    # cohérence INSEE/CP : on privilégie le citycode (corse, etc.)
                    cp = citycode[:2] + cp[2:] if len(cp) == 5 else cp
    except Exception:
        cp = None
    _CP_CACHE[key] = cp
    return cp


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = LIST_URL_P1 if page == 1 else LIST_URL_PN.format(page=page)
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[SudLoire] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.product")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                try:
                    bien = await _parse_card(card, client, departements)
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
                results.append(bien)
                new_on_page += 1

            print(f"[SudLoire] Page {page}: {new_on_page} biens retenus")

            # page suivante identique (clamp en fin de pagination) → stop
            if new_on_page == 0 and page > 1:
                # peut être une page sans bien dans la zone ; on continue un peu
                pass
            await asyncio.sleep(0.6)

    print(f"[SudLoire] Total : {len(results)} biens (zone cible)")
    return results


async def _parse_card(
    card, client: httpx.AsyncClient, departements: set[str]
) -> dict | None:
    link = card.select_one("a.product-image")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = _abs(href)

    # Titre + ville depuis .product-name ("<titre> , <Ville>")
    name_el = card.select_one(".product-name")
    spans = [s.get_text(" ", strip=True) for s in name_el.select("span")] if name_el else []
    spans = [s for s in spans if s and s != ","]
    if spans:
        ville = spans[-1].strip()
        titre = " ".join(spans[:-1]).strip(" ,") or spans[-1]
    else:
        full = name_el.get_text(" ", strip=True) if name_el else ""
        ville = full.split(",")[-1].strip() if "," in full else ""
        titre = full
    titre = titre.rstrip(" ,").strip()

    # Type de bien : slug d'URL + titre
    slug = href.split("/")[-1].replace("-", " ").replace(".html", "")
    blob = f"{titre} {slug}"
    if _EXCLUDE_TYPE.search(blob) and not _KEEP_TYPE.search(blob):
        return None
    if not _KEEP_TYPE.search(blob):
        # type ambigu → on garde quand même (la plupart sont des maisons/pavillons)
        type_bien = "maison"
    else:
        m = _KEEP_TYPE.search(blob)
        type_bien = m.group(0).lower() if m else "maison"

    # Résolution département via la ville (BAN)
    code_postal = await _ville_to_cp(client, ville) or ""
    dept = code_postal[:2] if code_postal else ""
    if not dept or dept not in departements:
        return None  # post-filtre STRICT : hors zone → écarté

    # Prix
    price_el = card.select_one(".product-price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Pièces / surface / réf via data-list
    pieces = _data_value(card, "NbPiece", to_int=True)
    surface = _data_value(card, "Surface", to_float=True)
    ref = _data_value(card, "products_model")

    # id_annonce : id numérique du segment d'URL (…_60149651/…) sinon réf sinon url
    m = re.search(r"_(\d{5,})/", href)
    id_annonce = (m.group(1) if m else "") or ref or url

    # Photos
    photos: list[str] = []
    for img in card.select("a.product-image img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(_abs(src))
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "sud_loire_immobilier",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Sud Loire Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _abs(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return BASE_URL + "/" + href.lstrip("./").lstrip("/")


def _data_value(card, kind: str, to_int=False, to_float=False):
    el = card.select_one(
        f".data-list__item--{kind} .data-list__item--value"
    )
    if not el:
        return None
    raw = el.get_text(" ", strip=True)
    if to_int or to_float:
        cleaned = re.sub(r"[^\d.,]", "", raw).replace(",", ".")
        try:
            f = float(cleaned)
            return int(f) if to_int else f
        except ValueError:
            return None
    return raw or None


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d].*$", "", cleaned)  # coupe avant "dont X% honoraires"
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
    print(f"\nTotal Sud Loire Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

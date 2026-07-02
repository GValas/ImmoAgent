"""scrapers/village_immobilier.py — Le Village Immobilier (Aubigny-sur-Nère, Cher 18)

Agence indépendante d'Aubigny-sur-Nère couvrant le nord du Cher (18) et le sud du
Loiret (45) limitrophe (Sologne / Berry / Sancerrois). Site SSR (CMS « Polaris /
PowerBoutique ») : toutes les annonces sont dans le HTML brut → httpx pur.

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /annonces/transaction/Vente.html?...&page=N   (page 1 sans param)
              → AUCUN filtre dept côté serveur, et la carte n'affiche que le NOM de
                la commune (pas le CP). Filtre dept = POST-FILTRE STRICT via une
                table commune → code postal construite à partir du <select> de
                communes de la page (chaque option = "CP NOM-COMMUNE", ex.
                "18700 AUBIGNY-SUR-NERE"). Cette table est l'inventaire exact de
                l'agence → mapping fiable, 0 fuite (vérifié : 100 % des cartes
                résolues, uniquement 18/45).

Filtre département (0 fuite) :
  - on parse le <select> de communes → {commune_normalisée: CP} ;
  - pour chaque carte, on lit la commune (.product-name dernier <span>) et on
    récupère son CP dans la table ; on n'accepte que si CP[:2] ∈ cibles.
  - une commune absente de la table (cas théorique) est rejetée (prudence).

Cartes : div.product
  - URL    : a.product-image[href]  → ../fiches/{...}_{id}/{slug}.html
  - Titre  : .product-name (1ᵉʳ span) ; commune = dernier span
  - Prix   : .product-price  → "360 000 € dont 5.88% TTC d'honoraires"
  - Pièces : .data-list__item--NbPiece .data-list__item--value
  - Surface: .data-list__item--Surface .data-list__item--value
  - Réf    : .data-list__item--products_model .data-list__item--value
  - Photos : a.product-image img.photo[src] (+ .photo-hidden)

Type de bien : déduit du titre ; on ne garde que maisons / propriétés / fermes…,
               on exclut appartements / terrains / locaux / immeubles.

Couverture : Cher (18) + Loiret (45) (les autres départements cibles → 0 bien).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import unicodedata

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.village-immobilier.com"
LIST_PATH = "/annonces/transaction/Vente.html"
MAX_PAGES = 10
PHOTOS_PER_CARD = 4


_KEEP_TYPE = re.compile(
    r"maison|villa|propri[eé]t[eé]|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|mas|g[iî]te|corps de ferme|pavillon|b[aâ]tisse",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|hangar|studio|loft|grange seule|murs commerciaux",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _build_cp_map(html: str) -> dict[str, str]:
    """Construit {commune_normalisée: CP} depuis le <select> de communes."""
    cp_map: dict[str, str] = {}
    for cp, ville in re.findall(r'<option value="(\d{5})\s+([^"<]+)"', html):
        cp_map.setdefault(_norm(ville), cp)
    return cp_map


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()
    cp_map: dict[str, str] = {}

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            if page == 1:
                url = f"{BASE_URL}{LIST_PATH}"
            else:
                url = (
                    f"{BASE_URL}{LIST_PATH}"
                    f"?manufacturers_id=transaction&page={page}&search_id=&sort="
                )
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[VillageImmo] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            if not cp_map:
                cp_map = _build_cp_map(r.text)

            cards = BeautifulSoup(r.text, "html.parser").select("div.product")
            if not cards:
                break

            new_ids = 0
            for card in cards:
                try:
                    bien = _parse_card(card, cp_map)
                except Exception:
                    continue
                if not bien:
                    continue

                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                new_ids += 1

                # POST-FILTRE STRICT — 0 fuite hors-zone
                cp = bien["code_postal"]
                if not cp or cp[:2] not in departements:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                results.append(bien)

            if new_ids == 0:
                break

            await asyncio.sleep(0.5)

    print(f"[VillageImmo] {len(results)} annonces (depts {sorted({b['departement'] for b in results}) or '∅'})")
    return results


def _parse_card(card, cp_map: dict[str, str]) -> dict | None:
    link = card.select_one("a.product-image") or card.find("a", href=True)
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href.replace("../", f"{BASE_URL}/", 1) if href.startswith("../") else (
        href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"
    )

    m_id = re.search(r"_(\d+)/", href) or re.search(r"_(\d+)\b", href)
    id_url = m_id.group(1) if m_id else ""

    # Titre + commune
    name_spans = card.select(".product-name span")
    title_parts = [s.get_text(" ", strip=True) for s in name_spans]
    title_parts = [t for t in title_parts if t and t != ","]
    titre = title_parts[0] if title_parts else ""
    ville = title_parts[-1] if len(title_parts) > 1 else ""
    if not ville:
        return None

    if _EXCLUDE_TYPE.search(titre) and not _KEEP_TYPE.search(titre):
        return None
    if not _KEEP_TYPE.search(titre):
        return None
    type_bien = _deduce_type(titre) or "maison"

    # CP fiable via la table communes
    code_postal = cp_map.get(_norm(ville), "")
    if not code_postal:
        return None

    # Réf
    ref = ""
    ref_el = card.select_one(
        ".data-list__item--products_model .data-list__item--value"
    )
    if ref_el:
        ref = ref_el.get_text(" ", strip=True)
    id_annonce = ref or id_url or url

    price_el = card.select_one(".product-price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    pieces = _data_int(card, "NbPiece")
    surface = _data_float(card, "Surface")

    photos = []
    for img in card.select("a.product-image img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            src = src.replace("../", f"{BASE_URL}/", 1) if src.startswith("../") else src
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "village_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Le Village Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _deduce_type(text: str) -> str:
    m = _KEEP_TYPE.search(text or "")
    return m.group(0).lower() if m else ""


def _data_int(card, suffix: str) -> int | None:
    el = card.select_one(
        f".data-list__item--{suffix} .data-list__item--value"
    )
    if el:
        m = re.search(r"(\d+)", el.get_text(strip=True))
        if m:
            return int(m.group(1))
    return None


def _data_float(card, suffix: str) -> float | None:
    el = card.select_one(
        f".data-list__item--{suffix} .data-list__item--value"
    )
    if el:
        m = re.search(r"(\d+(?:[.,]\d+)?)", el.get_text(strip=True))
        if m:
            try:
                f = float(m.group(1).replace(",", "."))
                if 1 <= f <= 100000:
                    return f
            except ValueError:
                pass
    return None


def _parse_price(text: str) -> float | None:
    # "360 000 € dont 5.88% TTC..." → on prend le 1ᵉʳ montant avant €
    m = re.search(r"([\d\s\xa0]+)\s*€", text)
    if not m:
        return None
    raw = re.sub(r"[^\d]", "", m.group(1))
    try:
        v = float(raw) if raw else None
    except ValueError:
        return None
    if v and v < 1000:
        return None
    return v


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
    print(f"\nTotal Le Village Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

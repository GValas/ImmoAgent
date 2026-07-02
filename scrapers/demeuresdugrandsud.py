"""scrapers/demeuresdugrandsud.py — Demeures du Grand Sud (agence de prestige Provence)

Méthode : scrape_simple (httpx) — SSR HTML complet (prix/CP en clair dans le HTML).
Site : agence unique haut de gamme (mas, bastides, châteaux, propriétés) implantée
       en Provence (Vaucluse 84, Gard 30, Drôme 26, Alpes-de-Haute-Provence 04).

URL pattern (listing paginé global, PAS de filtre département serveur) :
    /vente/{page}            → ex /vente/1, /vente/2 … (~10 biens/page, ~5 pages)
URL détail :
    /vente/{commune-id-slug}/{type}/{tN}/{id-slug}/
    ex : /vente/6-orange/mas/t8/664-mas-authentique/

⚠️ Le site filtre par COMMUNE (slug dans l'URL détail), jamais par département.
   → POST-FILTRE STRICT sur code_postal[:2] indispensable (objectif 0 fuite).
   La zone réelle du site (84/30/26/04) est HORS des départements cibles actuels
   (72/28/45/89/…), donc 0 stock attendu ; scraper conservé pour réactivation.

Cartes : article.property-listing-v2__item
  - Ville  : .title__content-1          → "Orange"
  - CP     : .title__content-2          → "(84100)"
  - Compo  : .property-listing-v2__item-compo  → "8 pièces - 340 m²"
  - Titre  : h2 a.property-listing-v2__item-text span
  - URL    : h2 a[href]  (segment {type} déduit du chemin)
  - Prix   : .property-listing-v2__price-value  → "1 295 000 €"
  - Réf    : .property-listing-v2__item-reference  → "Ref : V50000664"
  - Photos : img.item__img[data-src]  (CDN staticlbi, // → https:)

Type de bien : déduit du segment d'URL (mas, propriete, chateau, maison…).
               On ne garde que maisons/propriétés/demeures (pas appart/terrain).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int

BASE_URL = "https://www.demeuresdugrandsud.com"
MAX_PAGES = 12
PHOTOS_PER_CARD = 10


# Types de bien (segment d'URL) à conserver : maisons / propriétés / demeures…
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|bastide|gite|gîte|corps-de-ferme|"
    r"maison-de-village",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=40
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/vente/{page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[DemeuresGrandSud] Erreur page {page}: {e}")
                await asyncio.sleep(0.6)
                continue
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                "article.property-listing-v2__item"
            )
            if not cards:
                break

            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # POST-FILTRE DÉPARTEMENT STRICT (le site ne filtre pas par dept)
                cp = bien["code_postal"]
                if not cp or cp[:2] not in departements:
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

                bien["departement"] = cp[:2]
                seen_ids.add(aid)
                results.append(bien)

            await asyncio.sleep(0.6)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print(f"[DemeuresGrandSud] {len(results)} annonces — par dept: {by_dept or '∅'}")

    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("h2 a[href]") or card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL : /vente/{commune}/{type}/{tN}/{id-slug}/
    parts = [p for p in href.split("/") if p]
    # parts ≈ ['vente', '6-orange', 'mas', 't8', '664-mas-authentique']
    type_seg = parts[2] if len(parts) > 2 else ""
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg.replace("-", " ").strip() or "maison"

    # Référence (id_annonce) : "Ref : V50000664"
    ref_el = card.select_one(".property-listing-v2__item-reference")
    ref = ""
    if ref_el:
        ref = re.sub(r"^\s*Ref\s*:?\s*", "", ref_el.get_text(" ", strip=True), flags=re.I)
    id_num = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_num = m.group(1)
    id_annonce = ref or id_num or url

    # Ville + CP
    ville_el = card.select_one(".title__content-1")
    cp_el = card.select_one(".title__content-2")
    ville = ville_el.get_text(" ", strip=True) if ville_el else ""
    cp_raw = cp_el.get_text(" ", strip=True) if cp_el else ""
    m_cp = re.search(r"(\d{5})", cp_raw)
    code_postal = m_cp.group(1) if m_cp else ""

    # Titre
    title_el = card.select_one("h2 a") or card.select_one("h2")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Compo : "8 pièces - 340 m²"
    compo_el = card.select_one(".property-listing-v2__item-compo")
    compo = compo_el.get_text(" ", strip=True) if compo_el else ""
    pieces = _parse_int(r"(\d+)\s*pi[eè]ces?", compo)
    surface = _parse_surface(compo)

    # Prix
    price_el = card.select_one(".property-listing-v2__price-value")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Photos (CDN)
    photos = []
    for img in card.select("img.item__img, img.js-lazy"):
        src = img.get("data-src") or img.get("data-path") or img.get("src") or ""
        if not src or src.startswith("data:"):
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = BASE_URL + src
        photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "demeuresdugrandsud",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2] if code_postal else None,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Demeures du Grand Sud",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text.replace("\xa0", ""))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'8 pièces - 340 m²' → 340.0"""
    m = re.search(r"([\d\s\xa0]+)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 5000:
                return f
        except ValueError:
            pass
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
    print(f"\nTotal Demeures du Grand Sud: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

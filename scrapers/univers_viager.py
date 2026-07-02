"""scrapers/univers_viager.py — Univers Viager (réseau national viager / vente à terme)

Méthode : scrape_simple (httpx) — SSR HTML (nginx, contenu dans le HTML brut).
URL pattern : /nos-annonces/  puis  /nos-annonces/page/{N}/  (pagination jusqu'à ~50 pages)
              → liste NATIONALE, pas de paramètre département exposé dans l'URL
                (le filtre "Département/région/ville" du site n'expose pas de query string)
              → on scrape le national paginé + POST-FILTRE strict code_postal[:2] == dept.

Segments : viager occupé, viager libre, vente à terme, nue-propriété.

Cartes : div.block-listings
  - URL    : a.goods-content-image[href]  (→ /bien/{slug}/)  ; secours a.btn-blue[href]
  - Type   : span.type-sale  → "Viager occupé" / "Viager libre" / "Vente à terme"...
  - Titre  : h2.goods-content-title
  - Loc    : p.city  →  "44053 Drefféac"  (code = INSEE 5 chiffres ; [:2] = département fiable)
  - Surface: span[itemprop=floorSize]  →  "78m2"
  - Chambres: span[itemprop=numberOfBedrooms]  →  "3 chambres"
  - Prix   : span[itemprop=price]  →  "118 000 €"  (= BOUQUET en viager)
             valeur du bien : .value-2 .price  →  "240 000 €"
  - Photos : a.goods-content-image img[src]

Prix retenu : le bouquet (span[itemprop=price]). En viager le "prix" affiché est le
bouquet versé à la signature — c'est lui qu'on met dans `prix`. La valeur vénale du
bien est ajoutée en fin de description pour contexte.

Filtre dept : aucun param serveur → scrape national + post-filtre code_postal[:2].
              On s'arrête tôt si aucun département cible n'est encore atteint et qu'on
              a parcouru tout le stock (pages vides).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.univers-viager.fr"
MAX_PAGES = 50
PHOTOS_PER_CARD = 6


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_urls: set[str] = set()
    per_dept: dict[str, int] = {}

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = (
                f"{BASE_URL}/nos-annonces/"
                if page == 1
                else f"{BASE_URL}/nos-annonces/page/{page}/"
            )
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[UniversViager] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.block-listings")
            if not cards:
                break

            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                cp = bien["code_postal"]
                # Post-filtre département STRICT (0 fuite)
                if not cp or cp[:2] not in departements:
                    continue

                if bien["url"] in seen_urls:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                seen_urls.add(bien["url"])
                results.append(bien)
                per_dept[cp[:2]] = per_dept.get(cp[:2], 0) + 1

            await asyncio.sleep(0.5)

    for dept in sorted(departements):
        print(f"[UniversViager] Dept {dept}: {per_dept.get(dept, 0)} annonces")

    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a.goods-content-image") or card.select_one(
        "a.btn-blue[href]"
    )
    href = link.get("href", "") if link else ""
    if not href or "/bien/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Localisation : "44053 Drefféac" (le code est l'INSEE 5 chiffres ; [:2] = dept)
    city_el = card.select_one("p.city")
    loc = city_el.get_text(" ", strip=True) if city_el else ""
    code_postal, ville = _parse_loc(loc)

    # Type de vente (viager occupé / libre / vente à terme / nue-propriété)
    type_el = card.select_one("span.type-sale")
    type_bien = type_el.get_text(" ", strip=True) if type_el else "Viager"

    # Titre
    title_el = card.select_one("h2.goods-content-title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien} {ville}".strip()

    # Surface habitable : "78m2"
    surf_el = card.select_one("span[itemprop=floorSize]")
    surface = _parse_surface(surf_el.get_text(" ", strip=True) if surf_el else "")

    # Chambres : "3 chambres"
    bed_el = card.select_one("span[itemprop=numberOfBedrooms]")
    chambres = _parse_int(bed_el.get_text(" ", strip=True) if bed_el else "")

    # Prix = bouquet (span[itemprop=price])
    price_el = card.select_one("span[itemprop=price]")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Valeur du bien (contexte) → ajoutée à la description
    val_el = card.select_one(".value-2 .price")
    valeur = _parse_price(val_el.get_text(" ", strip=True) if val_el else "")
    description = f"{type_bien}."
    if prix:
        description += f" Bouquet : {int(prix)} €."
    if valeur:
        description += f" Valeur du bien : {int(valeur)} €."

    # id_annonce : slug du /bien/
    slug = [p for p in href.split("/") if p][-1]
    id_annonce = slug or url

    # Photos
    photos = []
    for img in card.select("a.goods-content-image img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "univers_viager",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else None,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Univers Viager",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'44053 Drefféac' → ('44053', 'Drefféac')"""
    cp = ""
    m = re.search(r"\b(\d{5})\b", text)
    if m:
        cp = m.group(1)
    ville = re.sub(r"\b\d{5}\b", "", text).strip()
    return cp, ville


def _parse_int(text: str) -> int | None:
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def _parse_surface(text: str) -> float | None:
    """'78m2' → 78.0"""
    m = re.search(r"(\d[\d\s\xa0]*)\s*m", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 2000:
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
    print(f"\nTotal Univers Viager: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

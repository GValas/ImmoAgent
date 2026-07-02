"""scrapers/startgoimmo.py — StartGo Immobilier (réseau Occitanie, siège Fabrègues 34)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress + thème Houzez)
URL pattern : /property-type/{type}/                (page 1)
              /property-type/{type}/page/{n}/        (pages suivantes)
              → taxonomie Houzez par TYPE de bien (pas de filtre département serveur).
              Chaque annonce a une URL slug auto-portée :
              /annonces/vente-{type}-{CP}-{ville}-startgo-ref-{N}/
              → le code postal est DANS l'URL → post-filtre CP[:2] fiable (0 fuite).

Cartes liste : article.listings (thème Houzez)
  - URL + titre : a[href*="ref-"]  (1ʳᵉ occurrence)
  - CP / ville / type / ref : extraits du slug d'URL (regex)
  - Excerpt : div.excerpt  (description courte)
  - Photos  : ul.slides li img / data-thumb

Détail (fetch uniquement pour les biens retenus après filtre dept) :
  - Prix    : .price            → "465 000 €"
  - Méta    : ul.propinfo li    → ["112m²", "4 151,79 €", "Maison", "Property ID # A68501"]
              (surface habitable + prix/m² + type + ref ; pas de chambres/pièces fiables)

Couverture observée (2026-06-09) : stock 100 % Occitanie / sud — départements vus
  30, 34 (gros volume), + sporadique 22, 35, 38, 46, 74, 82. AUCUN bien dans la
  zone cible Val-de-Loire (72, 28, 45, 89, 49, 37, 36, 18, 58, 41, 53).
  → scraper fonctionnel mais 0 stock zone : conservé en actif:false dans sources.yaml.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.startgoimmo.fr"
MAX_PAGES = 10
PHOTOS_PER_CARD = 10

# Types de bien Houzez à parcourir (on ne garde que maisons/villas/propriétés ensuite)
PROPERTY_TYPES = ["maison", "villa", "propriete", "immeuble"]


# /annonces/vente-{type}-{CP}-{ville}-startgo-ref-{N}/
_URL_RE = re.compile(
    r"/annonces/vente-([a-z]+)-(\d{5})-(.+?)-startgo-ref-(\d+)/?$", re.IGNORECASE
)

_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    dept_set = set(departements)
    results: list[dict] = []
    seen_ref: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        # 1) Collecte des cartes liste (toutes pages, tous types) → ne retient que
        #    les annonces dont le CP (extrait du slug) est dans la zone cible.
        candidates: list[dict] = []
        for ptype in PROPERTY_TYPES:
            for page in range(1, MAX_PAGES + 1):
                if page == 1:
                    url = f"{BASE_URL}/property-type/{ptype}/"
                else:
                    url = f"{BASE_URL}/property-type/{ptype}/page/{page}/"
                try:
                    r = await client.get(url)
                except Exception as e:
                    print(f"[StartGo] Erreur {ptype} p{page}: {e}")
                    break
                if r.status_code != 200:
                    break

                cards = BeautifulSoup(r.text, "html.parser").select("article.listings")
                if not cards:
                    break

                found = 0
                for card in cards:
                    base = _parse_card(card)
                    if not base:
                        continue
                    found += 1
                    ref = base["id_annonce"]
                    if ref in seen_ref:
                        continue
                    # Post-filtre département STRICT (0 fuite)
                    if base["code_postal"][:2] not in dept_set:
                        continue
                    # Filtre type (maisons / propriétés uniquement)
                    if _EXCLUDE_TYPE.search(base["type_bien"]) and not _KEEP_TYPE.search(
                        base["type_bien"]
                    ):
                        continue
                    if not _KEEP_TYPE.search(base["type_bien"]):
                        continue
                    seen_ref.add(ref)
                    candidates.append(base)

                if found == 0:
                    break
                await asyncio.sleep(0.5)

        # 2) Enrichissement détail (prix + surface) uniquement sur les biens retenus.
        for base in candidates:
            try:
                await _enrich_detail(client, base)
            except Exception as e:
                print(f"[StartGo] Détail KO {base['id_annonce']}: {e}")

            p = base.get("prix") or 0
            s = base.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue
            results.append(base)
            await asyncio.sleep(0.5)

    print(f"[StartGo] Total retenu zone : {len(results)} annonces")
    return results


def _parse_card(card) -> dict | None:
    link = None
    for a in card.select("a[href]"):
        if "-startgo-ref-" in (a.get("href") or ""):
            link = a
            break
    if not link:
        return None
    href = link.get("href", "")
    url = href if href.startswith("http") else BASE_URL + href

    m = _URL_RE.search(href)
    if not m:
        return None
    type_seg, cp, ville_slug, ref = m.groups()
    type_bien = type_seg.replace("-", " ").strip().lower()
    ville = ville_slug.replace("-", " ").strip().title()

    titre = link.get_text(" ", strip=True) or f"{type_bien.title()} {ville}"

    excerpt_el = card.select_one("div.excerpt")
    description = excerpt_el.get_text(" ", strip=True) if excerpt_el else ""
    description = re.sub(r"\s*Lire la suite\s*$", "", description).strip()

    # Photos : data-thumb (slides) puis src
    photos: list[str] = []
    for li in card.select("ul.slides li"):
        src = li.get("data-thumb") or ""
        if src:
            photos.append(src)
    if not photos:
        for img in card.select("img"):
            src = img.get("src") or ""
            if src and "wp-content/uploads" in src and not src.startswith("data:"):
                photos.append(src)
    # dédup en gardant l'ordre
    seen: set[str] = set()
    photos = [p for p in photos if not (p in seen or seen.add(p))][:PHOTOS_PER_CARD]

    # Surface terrain depuis l'excerpt si présente ("sur 440 m² de terrain")
    surface_terrain = _parse_terrain(description)
    surface = _parse_surface_hab(description)

    return {
        "source": "startgoimmo",
        "url": url,
        "id_annonce": ref,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": cp[:2],
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": None,
        "prix": None,
        "photos": photos,
        "dpe": None,
        "agence": "StartGo Immobilier",
    }


async def _enrich_detail(client: httpx.AsyncClient, base: dict) -> None:
    r = await client.get(base["url"])
    if r.status_code != 200:
        return
    s = BeautifulSoup(r.text, "html.parser")

    price_el = s.select_one(".price")
    if price_el:
        base["prix"] = _parse_price(price_el.get_text(" ", strip=True))

    propinfo = s.select_one("ul.propinfo")
    if propinfo:
        for li in propinfo.select("li"):
            t = li.get_text(" ", strip=True)
            # surface habitable : "112m²"
            ms = re.match(r"^([\d\s\xa0]+)\s*m²\s*$", t)
            if ms and base.get("surface") is None:
                val = re.sub(r"[\s\xa0]", "", ms.group(1))
                try:
                    f = float(val)
                    if 8 <= f <= 2000:
                        base["surface"] = f
                except ValueError:
                    pass

    # Photos détail en secours si la carte n'en avait pas
    if not base["photos"]:
        for img in s.select("ul.slides li a.gallery-item"):
            src = img.get("href") or ""
            if src and "wp-content/uploads" in src:
                base["photos"].append(src)
        base["photos"] = base["photos"][:PHOTOS_PER_CARD]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_terrain(text: str) -> float | None:
    """'sur 440 m² de terrain' → 440.0"""
    if not text:
        return None
    m = re.search(
        r"(\d[\d\s\xa0]*)\s*m²?\s*de\s+terrain", text, re.IGNORECASE
    ) or re.search(r"terrain[^\d]{0,15}(\d[\d\s\xa0]*)\s*m²", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 10 <= f <= 200000:
                return f
        except ValueError:
            pass
    return None


def _parse_surface_hab(text: str) -> float | None:
    if not text:
        return None
    m = re.search(
        r"(\d[\d\s\xa0]*)\s*m²?\s*(?:hab|habitable|de surface habitable)",
        text,
        re.IGNORECASE,
    )
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
    print(f"\nTotal StartGo: {len(biens)} annonces")
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

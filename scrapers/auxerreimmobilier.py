"""scrapers/auxerreimmobilier.py — Auxerre Immobilier (agence locale Auxerre / Yonne)

Méthode : scrape_simple (httpx) — SSR HTML (pas de JS).
Site mono-zone : agence locale d'Auxerre (89) couvrant exclusivement Auxerre
et son rayon (~30 km autour d'Auxerre, intégralement dans l'Yonne).

URL pattern (listing par catégorie, tout sur une seule page) :
    /auxerre-immobilier-{cat}-{nom}
        1-maisons, 2-pavillons, 3-appartements
    → un seul écran par catégorie (pas de pagination numérique ; seuls des
      filtres de zone/tri existent). On ne garde que maisons + pavillons.

Cartes : div.col-md-4.col-sm-6.col-xs-12 > a > section.bien
  - URL    : a[href]  →  /agence-immobiliere-auxerre-{ID}-{slug}
  - id     : segment numérique {ID} du slug ("auxerre-745-...")
  - Image  : div.box-image[style="background-image:url(content/...)"]
  - Titre  : h3.nom-bien
  - Prix   : div.prix  →  "327000 €"
  - Champs : dl/dt-dd  (Catégorie, Type "F6", Surface habitable "182 m²",
             Terrain "1108 m²", Zone "-10 km d'AUXERRE")

Filtre département — particularité :
  Le site n'expose NI code postal NI commune précise (seulement une "Zone"
  = bande de rayon autour d'AUXERRE, pour protéger les mandats). Toutes les
  zones observées sont des dérivés d'AUXERRE → 100 % département 89.
  Stratégie : le scraper ne tourne QUE si 89 est demandé ; chaque bien retenu
  doit avoir une Zone contenant "AUXERRE" (post-filtre strict, rejette toute
  zone hors-Auxerre éventuelle). departement="89" fixé ; code_postal=None
  (réellement indisponible). → 0 fuite hors 89 par construction.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.auxerreimmobilier.fr"
DEPT = "89"
PHOTOS_PER_CARD = 1  # seule l'image de carte est exposée dans le listing


# Catégories de listing à scraper : maisons + pavillons (on écarte appartements).
CATEGORIES = [
    ("1", "maisons", "maison"),
    ("2", "pavillons", "pavillon"),
]

# Toute zone retenue doit référencer AUXERRE (gage d'appartenance au 89).
_ZONE_OK = re.compile(r"auxerre", re.IGNORECASE)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Agence mono-zone : ne tourne que si l'Yonne (89) est dans la cible.
    if DEPT not in departements:
        print(f"[AuxerreImmo] Dept {DEPT} hors cible → skip")
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for cat, nom, type_bien in CATEGORIES:
            url = f"{BASE_URL}/auxerre-immobilier-{cat}-{nom}"
            try:
                cards = await _scrape_listing(client, url)
            except Exception as e:
                print(f"[AuxerreImmo] Erreur {nom}: {e}")
                continue

            kept = 0
            for card in cards:
                try:
                    bien = _parse_card(card, type_bien)
                except Exception:
                    continue
                if not bien:
                    continue

                # Post-filtre STRICT : la zone doit référencer AUXERRE (= 89).
                if not bien.pop("_zone_ok", False):
                    continue

                if bien["id_annonce"] in seen_ids:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                seen_ids.add(bien["id_annonce"])
                results.append(bien)
                kept += 1

            print(f"[AuxerreImmo] {nom}: {kept} annonces (89)")
            await asyncio.sleep(0.5)

    return results


async def _scrape_listing(client: httpx.AsyncClient, url: str) -> list:
    r = await client.get(url)
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    return soup.select("div.col-md-4.col-sm-6.col-xs-12")


def _parse_card(card, type_bien: str) -> dict | None:
    link = card.find("a")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

    # id : segment numérique du slug → "...-auxerre-745-..."
    m_id = re.search(r"auxerre-(\d+)-", href)
    id_annonce = m_id.group(1) if m_id else url

    title_el = card.select_one(".nom-bien")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    price_el = card.select_one(".prix")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Paires dt/dd
    fields: dict[str, str] = {}
    dts = card.select("dt")
    dds = card.select("dd")
    for dt, dd in zip(dts, dds):
        fields[dt.get_text(strip=True).lower()] = dd.get_text(strip=True)

    zone = fields.get("zone", "")
    surface = _parse_m2(fields.get("surface habitable", ""))
    surface_terrain = _parse_m2(fields.get("terrain", ""))
    pieces = _parse_pieces(fields.get("type", ""))

    if not titre:
        titre = f"{type_bien.title()} secteur Auxerre"

    # Image de fond de la carte
    photos = []
    img_el = card.select_one(".box-image")
    if img_el and img_el.get("style"):
        m_img = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", img_el["style"])
        if m_img:
            src = m_img.group(1).strip()
            if src and not src.startswith("data:"):
                photos.append(src if src.startswith("http") else f"{BASE_URL}/{src.lstrip('/')}")

    return {
        "source": "auxerreimmobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": DEPT,
        "ville": "Auxerre (secteur)",
        "code_postal": None,  # non exposé par le site
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Auxerre Immobilier",
        "_zone_ok": bool(_ZONE_OK.search(zone)),
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_m2(text: str) -> float | None:
    """'182 m²' / '1108 m²' → float."""
    if not text:
        return None
    m = re.search(r"([\d\s\xa0]+)", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
        return f if f > 0 else None
    except ValueError:
        return None


def _parse_pieces(text: str) -> int | None:
    """'F6' / 'T4' → 6 / 4."""
    m = re.search(r"[FT](\d+)", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


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
    print(f"\nTotal Auxerre Immobilier: {len(biens)} annonces")
    # code_postal=None ici → on contrôle la fuite via le champ 'departement'.
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['pieces'] or '?'}p — {b['type_bien']}"
        )

"""scrapers/guedon_immobilier.py — Guédon Immobilier (agences locales 53/49)

Méthode : scrape_simple (httpx) — SSR HTML (CMS staticlbi, type "la petite agence").
Site : agences de Laval / Meslay-du-Maine / Château-Gontier (53) + Angers (49).

URL pattern : /vente/{page}   (inventaire NATIONAL non filtré, ~3 pages, ~44 biens)
  ⚠️ Les URLs /vente/{NN-dept-slug}/{page} ne FILTRENT PAS réellement :
     /vente/72-sarthe/1 renvoie le même stock 53 que /vente/1 (slug cosmétique).
     → On crawle l'inventaire global et on POST-FILTRE STRICT code_postal[:2].
  Stock réel observé : ~43 biens en 53 (Mayenne) + qq 49/72. Couverture des
  départements cibles (72/28/45/89) quasi nulle → scraper conservé, post-filtre
  garantit 0 fuite.

Cartes : article.item
  - URL    : a[href*="/vente/"]  → /vente/{seg}/{type}/{id}-{slug}
  - Type   : 3ᵉ segment d'URL (maison, appartement, terrain-de-loisir, propriete…)
  - Loc    : .title-v1  →  "Ville (CODEPOSTAL)"
  - Prix   : .item__price  →  "220 500 €"
  - Options: .option__number  (valeurs non étiquetées par icône SVG ; la valeur
             en "m²" est le TERRAIN — vérifié page détail : habitable=120, m²-carte=497
             = "terrain clos de 497 m²". La surface HABITABLE n'est pas fiable sur
             la carte → surface_terrain renseigné, surface=None.)
  - Photos : picture.media-js source[srcset] / img[src]  (//guedon-immo.staticlbi.com)
  - id     : segment numérique du slug de l'URL détail

Type de bien : déduit du segment d'URL. On ne garde que maisons / propriétés.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.guedon-immobilier.com"
MAX_PAGES = 10
PHOTOS_PER_CARD = 10


# Types de bien (segment d'URL) à conserver : maisons / propriétés / fermes...
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|maison-de-village",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/vente/{page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Guedon] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("article.item")
            if not cards:
                break

            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # POST-FILTRE DÉPARTEMENT STRICT (slug URL non fiable) → 0 fuite
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

                seen_ids.add(aid)
                results.append(bien)

            await asyncio.sleep(0.6)

    print(f"[Guedon] {len(results)} annonces retenues sur {sorted(departements)}")
    return results


def _parse_card(card) -> dict | None:
    link = None
    for a in card.select("a[href]"):
        href = a.get("href", "")
        if "/vente/" in href and re.search(r"/vente/[^/]+/[^/]+/\d+", href):
            link = href
            break
    if not link:
        return None
    url = link if link.startswith("http") else BASE_URL + link

    parts = [p for p in link.split("/") if p]
    # /vente/{seg}/{type}/{id-slug}
    type_seg = parts[2] if len(parts) > 2 else ""
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg.replace("-", " ").strip() or "maison"

    # id_annonce : segment numérique du dernier segment d'URL
    id_annonce = url
    m = re.match(r"^(\d+)-", parts[-1]) if parts else None
    if m:
        id_annonce = m.group(1)

    # Localisation : "Ville (CODEPOSTAL)"
    loc_el = card.select_one(".title-v1, .item__block--city")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)

    titre = loc or type_bien.title()

    # Prix
    price_el = card.select_one(".item__price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # La valeur en "m²" des options correspond au TERRAIN (vérifié page détail).
    # La surface habitable n'est pas exposée de façon fiable sur la carte.
    surface_terrain = None
    for opt in card.select(".option__number"):
        txt = opt.get_text(" ", strip=True)
        m_s = re.search(r"(\d[\d\s\xa0]*)\s*m²", txt)
        if m_s:
            val = re.sub(r"[\s\xa0]", "", m_s.group(1))
            try:
                f = float(val)
                if 8 <= f <= 200000:
                    surface_terrain = f
                    break
            except ValueError:
                pass

    # Photos
    photos: list[str] = []
    for pic in card.select("picture.media-js"):
        src = ""
        source = pic.find("source")
        if source and source.get("srcset"):
            src = source.get("srcset").split(",")[0].split(" ")[0]
        if not src:
            img = pic.find("img")
            src = img.get("src", "") if img else ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "guedon_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2] if code_postal else None,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": None,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Guédon Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Argentré (53210)' → ('Argentré', '53210')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


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
    print(f"\nTotal Guédon Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

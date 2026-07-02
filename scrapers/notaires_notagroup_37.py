"""scrapers/notaires_notagroup_37.py — NOTA GROUP (SELARL AZAY NOTA GROUP, Azay-le-Rideau 37)

Méthode : scrape_simple (httpx) — SSR HTML (gabarit immonot/notariat.services).
URL pattern : /annonces-immobilieres/recherche.html
              → office notarial mono-secteur (Azay-le-Rideau & alentours), 100%
                Indre-et-Loire (37). Pas de param département serveur ; le stock
                est nativement 37 → post-filtre strict sur le CP quand même.
              → liste sur UNE seule page (~18 annonces, pas de pagination réelle :
                ?page=2 renvoie 0 carte). MAX_PAGES = 1 par sécurité.

Cartes : div.ns-property-card
  - URL    : a[href]  → /annonces-immobilieres/annonce/{id}__{slug}/...-{ville}-{cp}-...html
  - Type   : h2.c__type  → "Achat - Maison" (segment après le tiret)
  - Loc    : .c__location  → "Azay-le-Rideau - 37190"
  - Prix   : .c__price b  → "116 600  €"
  - Excerpt: .c__excerpt (description courte)
  - Réf    : .prop__reference  → "Réf: 054/122"
  - Quickinfos (li.qi__bubble dans .c__quickinfos) :
      fa-home  → surface habitable (m²)
      fa-leaf  → surface terrain (m²)
      data-tipso="Nombre de pièces" + <small>p</small>   → pièces
      data-tipso="Nombre de pièces" + <small>chb</small> → chambres
  - Photos : .slider-properties img.img-responsive[src]

Type de bien : on ne conserve que maisons / propriétés (exclut terrain, appartement,
               immeuble, local, parking…).

Couverture : office mono-département (37), faible volume (~6 maisons), 0 fuite.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.notagroup.notaires.fr"
SEARCH_URL = f"{BASE_URL}/annonces-immobilieres/recherche.html"
MAX_PAGES = 1
PHOTOS_PER_CARD = 10


# Types de bien à conserver (maisons / propriétés / fermes…)
_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|long[eè]re|manoir|chateau|ch[âa]teau|"
    r"moulin|demeure|domaine|mas|g[iî]te|corps[- ]de[- ]ferme|maison de village",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"viager|cave|cession",
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
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            params = {"page": page} if page > 1 else None
            try:
                r = await client.get(SEARCH_URL, params=params)
            except Exception as e:
                print(f"[NotaGroup37] Erreur requête page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(".ns-property-card")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # Post-filtre STRICT département (objectif : 0 fuite hors-zone)
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
                new_on_page += 1

            if new_on_page == 0:
                break

            await asyncio.sleep(0.5)

    print(f"[NotaGroup37] {len(results)} annonces (maisons/propriétés, 37)")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien : h2.c__type → "Achat - Maison" (la div .c__location est imbriquée)
    type_el = card.select_one("h2.c__type, .c__type")
    type_raw = ""
    if type_el:
        for child in type_el.children:
            if isinstance(child, str) and child.strip():
                type_raw = child.strip()
                break
    # "Achat - Maison" → "Maison"
    type_bien = re.sub(r"^.*?[-–]\s*", "", type_raw).strip() or type_raw.strip()
    if not type_bien:
        return None
    if _EXCLUDE_TYPE.search(type_bien) and not _KEEP_TYPE.search(type_bien):
        return None
    if not _KEEP_TYPE.search(type_bien):
        return None

    # Localisation : "Azay-le-Rideau - 37190"
    loc_el = card.select_one(".c__location")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)
    if not code_postal:
        # secours : CP dans le slug d'URL (...-azay-le-rideau-37190-...)
        m = re.search(r"-(\d{5})-", href)
        if m:
            code_postal = m.group(1)

    # Prix
    price_el = card.select_one(".c__price b")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Référence (id_annonce)
    ref_el = card.select_one(".prop__reference")
    ref = ""
    if ref_el:
        ref = re.sub(r"^R[ée]f\s*:\s*", "", ref_el.get_text(" ", strip=True),
                     flags=re.IGNORECASE).strip()
    # id interne dans le slug d'URL : /annonce/{id}__{token}/...
    id_num = ""
    m = re.search(r"/annonce/([^/]+)/", href)
    if m:
        id_num = m.group(1)
    id_annonce = ref or id_num or url

    # Description (excerpt)
    exc_el = card.select_one(".c__excerpt")
    description = exc_el.get_text(" ", strip=True) if exc_el else ""

    # Quickinfos : surface / terrain / pièces / chambres
    surface = surface_terrain = None
    pieces = chambres = None
    for li in card.select(".c__quickinfos li.qi__bubble"):
        text = li.get_text(" ", strip=True)
        if li.select_one("em.fa-home"):
            surface = _first_num(text)
        elif li.select_one("em.fa-leaf"):
            surface_terrain = _first_num(text)
        elif li.find("small") and "chb" in text.lower():
            chambres = _first_int(text)
        elif li.find("small") and re.search(r"\bp\b", text.lower()):
            pieces = _first_int(text)

    # Titre
    titre = f"{type_bien} {ville}".strip()

    # Photos
    photos = []
    for img in card.select(".slider-properties img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "notaires_notagroup_37",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien.lower(),
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "SELARL Azay Nota Group",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Azay-le-Rideau - 37190' → ('Azay-le-Rideau', '37190')"""
    cp = ""
    m_cp = re.search(r"(\d{5})", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"[-–]?\s*\d{5}\s*$", "", text).strip(" -–").strip()
    return ville, cp


def _first_num(text: str) -> float | None:
    m = re.search(r"([\d\s\xa0]+)", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        return float(val) if val else None
    except ValueError:
        return None


def _first_int(text: str) -> int | None:
    m = re.search(r"(\d+)", text)
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
    print(f"\nTotal NotaGroup37: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']} — {len(b['photos'])} photos"
        )

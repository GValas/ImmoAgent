"""scrapers/neos_immo.py — Néos Immo (réseau de mandataires éco-responsable national)

Méthode : scrape_simple (httpx) — SSR HTML (markup type Tailwind + schema.org/RealEstateListing)
URL pattern : /recherche?page={N}
              → AUCUN filtre département côté serveur : la recherche est nationale.
              → POST-FILTRE STRICT sur code_postal[:2] (0 fuite hors-zone).

Cartes : div.shadow contenant a[href*="/annonce/"], avec attributs schema.org itemprop :
  - URL    : a[itemprop="url"][href]            → /annonce/Vente-Maison-{ville}-{id}
  - Photo  : img[src*="/uploads/ads/"]
  - Titre  : h2[itemprop="name"]                 → "Vente - Maison - 4 pièce(s)"
  - Loc    : p[itemprop="address"]               → "Pagny-la-Ville - 21250"
  - Pièces : [itemprop="numberOfRooms"]          → "4 pièce(s)"
  - Cham.  : [itemprop="numberOfBedrooms"]       → "3 chambre(s)"
  - Surface: [itemprop="floorSize"]              → "100 m²"
  - Prix   : [itemprop="price"][content]         → "165000"
  - Terrain: li[title*="terrain"] (quand présent) → "1200 m²"
  - id     : data-identifier sur la carte (sinon hash final de l'URL)

Type de bien : déduit du titre / segment d'URL (Maison / Appartement / Terrain…).
               On ne conserve que maisons / propriétés (exclut appartement/terrain/commerce…).

Couverture : réseau national à implantation inégale ; ~372 biens nationaux, 30/page.
             Filtre dept par balayage des pages + post-filtre CP[:2].

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://neos-immo.com"
MAX_PAGES = 20          # ~372 biens / 30 par page ≈ 13 pages ; marge de sécurité
PHOTOS_PER_CARD = 1     # la carte liste n'expose qu'une vignette


# Types de bien à conserver (maisons / propriétés)
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|pavillon",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"viager|cave|box",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()
    per_dept: dict[str, int] = {}

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/recherche?page={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[NeosImmo] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                'div.shadow:has(a[href*="/annonce/"])'
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

                # POST-FILTRE STRICT département : 0 fuite hors-zone
                cp = bien.get("code_postal") or ""
                dept = cp[:2]
                if dept not in departements:
                    continue
                bien["departement"] = dept

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
                per_dept[dept] = per_dept.get(dept, 0) + 1

            await asyncio.sleep(0.5)

    for d in sorted(per_dept):
        print(f"[NeosImmo] Dept {d}: {per_dept[d]} annonces")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one('a[href*="/annonce/"]')
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien : depuis titre / segment d'URL (Vente-Maison-Ville-id)
    title_el = card.select_one('h2[itemprop="name"]')
    titre_raw = title_el.get_text(" ", strip=True) if title_el else ""
    titre = re.sub(r"\s+", " ", titre_raw).strip()

    type_source = titre + " " + href
    if _EXCLUDE_TYPE.search(type_source) and not _KEEP_TYPE.search(titre):
        return None
    if not _KEEP_TYPE.search(type_source):
        return None
    m_type = _KEEP_TYPE.search(type_source)
    type_bien = (m_type.group(0).lower() if m_type else "maison").replace("é", "e")

    # Localisation : "Pagny-la-Ville - 21250"
    addr_el = card.select_one('p[itemprop="address"]')
    loc = addr_el.get_text(" ", strip=True) if addr_el else ""
    ville, code_postal = _parse_loc(loc)
    if not code_postal:
        # secours : CP dans l'alt de l'image  ("... à Pagny-la-Ville (21250)")
        img = card.select_one('img[src*="/uploads/ads/"]')
        alt = img.get("alt", "") if img else ""
        m = re.search(r"\((\d{5})\)", alt)
        if m:
            code_postal = m.group(1)
            if not ville:
                mv = re.search(r"à\s+(.+?)\s*\(\d{5}\)", alt)
                ville = mv.group(1).strip() if mv else ville

    # id_annonce : data-identifier sinon hash final de l'URL
    aid = card.get("data-identifier", "")
    if not aid:
        m = re.search(r"-([0-9a-f]+)$", href)
        aid = m.group(1) if m else url

    # Prix : itemprop=price (attribut content) ou texte
    price_el = card.select_one('[itemprop="price"]')
    prix = None
    if price_el:
        prix = _parse_price(price_el.get("content") or price_el.get_text(" ", strip=True))

    # Surface habitable
    floor_el = card.select_one('[itemprop="floorSize"]')
    surface = _parse_num(floor_el.get_text(" ", strip=True)) if floor_el else None

    # Pièces / chambres
    rooms_el = card.select_one('[itemprop="numberOfRooms"]')
    pieces = _parse_int(rooms_el.get_text(" ", strip=True)) if rooms_el else None
    bed_el = card.select_one('[itemprop="numberOfBedrooms"]')
    chambres = _parse_int(bed_el.get_text(" ", strip=True)) if bed_el else None

    # Terrain : li dont le title mentionne "terrain"
    surface_terrain = None
    for li in card.select("li"):
        title = (li.get("title") or "").lower()
        if "terrain" in title:
            surface_terrain = _parse_num(li.get_text(" ", strip=True))
            break

    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Photo (vignette unique en vue liste)
    photos = []
    img = card.select_one('img[src*="/uploads/ads/"]')
    if img:
        src = img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "neos_immo",
        "url": url,
        "id_annonce": aid,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
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
        "agence": "Néos Immo",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Pagny-la-Ville - 21250' → ('Pagny-la-Ville', '21250')"""
    cp = ""
    m = re.search(r"(\d{5})", text)
    if m:
        cp = m.group(1)
    ville = re.sub(r"\s*-?\s*\d{5}\s*$", "", text).strip(" -").strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    if text is None:
        return None
    cleaned = re.sub(r"[^\d]", "", str(text))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_num(text: str) -> float | None:
    """'100 m²' → 100.0 ; '1 200 m²' → 1200.0"""
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


def _parse_int(text: str) -> int | None:
    m = re.search(r"(\d+)", text or "")
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
    print(f"\nTotal Néos Immo: {len(biens)} annonces")
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

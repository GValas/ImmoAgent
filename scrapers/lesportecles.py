"""scrapers/lesportecles.py — Les Portecles (réseau de mandataires de proximité)

Méthode : scrape_simple (httpx) — SSR HTML (Rails / Ransack `q[...]`)
URL pattern : /achat?page=N
  → liste NATIONALE paginée (~6 cartes/page, ~342 pages).
  ⚠️ AUCUN filtre département serveur exploitable en httpx :
     - le param `?department=NN` vu dans certains liens NE filtre PAS les résultats ;
     - le formulaire utilise `q[geoloc]` (texte géocodé côté JS) + `q[r]` (rayon),
       inerte sans résolution lat/lng → ignoré sous httpx.
  → on crawle le national et on POST-FILTRE strictement sur code_postal[:2].

Cartes : li.list-group-item[itemscope] (microdata schema.org)
  - URL   : a[href]  → /achat/sale-...-{cp}-{ville}-...-{uuid}
  - Réf   : texte du 1er <a> ("Bien BXXXXX")
  - Titre : [itemprop=name]
  - Ville : [itemprop=addressLocality]
  - CP    : [itemprop=postalCode]   ← filtre département (cp[:2])
  - Dept  : [itemprop=addressRegion] (nom du département)
  - Desc  : [itemprop=description]
  - Prix  : .price span  →  "937 000 €"
  - Surface/pièces/chambres : .primaryinfo (NNN m² / N pièces / N chambres)
  - Photo : img[itemprop=image][src]
  - Geo   : meta[itemprop=latitude/longitude]

Type de bien : déduit du segment d'URL (sale-maison-villa, sale-appartement,
               sale-local, sale-immeuble, sale-entreprises, sale-terrain...).
               On ne conserve que maisons / villas / propriétés / fermes...

Couverture : réseau national à implantation inégale ; inventaire faible sur la
             zone Val-de-Loire / Ouest (post-filtre obligatoire, 0 fuite).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.lesportecles.com"
# ~342 pages au national ; on stoppe dès qu'une page est vide.
MAX_PAGES = 360
PHOTOS_PER_CARD = 10


# Types de bien (segment d'URL) à conserver : maisons / propriétés / fermes...
_KEEP_TYPE = re.compile(
    r"maison|villa|propriete|propriété|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|maison-de-village",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|entreprises|professionnel|viager",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()
    per_dept: dict[str, int] = {d: 0 for d in departements}

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/achat?page={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[LesPortecles] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                "li.list-group-item[itemscope]"
            )
            if not cards:
                # plus aucune annonce → fin de pagination
                break

            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # POST-FILTRE DÉPARTEMENT STRICT (national → zone) : 0 fuite
                cp = bien["code_postal"]
                if not cp or cp[:2] not in departements:
                    continue
                dept = cp[:2]
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
                per_dept[dept] += 1

            await asyncio.sleep(0.5)

    for dept in sorted(departements):
        print(f"[LesPortecles] Dept {dept}: {per_dept[dept]} annonces")
    return results


def _parse_card(card) -> dict | None:
    link = card.find("a", href=True)
    href = link.get("href", "") if link else ""
    if not href or "/achat/sale" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL : /achat/sale-{type}-...-{cp}-{ville}-...
    # (certaines URL n'ont pas de type : /achat/sale-{cp}-{ville} → type inconnu)
    type_seg = href.split("/achat/sale-", 1)[-1]
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None

    # Localisation (microdata)
    cp_el = card.select_one('[itemprop="postalCode"]')
    code_postal = cp_el.get_text(strip=True) if cp_el else ""
    m_cp = re.search(r"\b(\d{5})\b", code_postal)
    if not m_cp:
        # secours : CP dans le slug d'URL
        m_cp = re.search(r"sale-(?:[a-z-]+?-)?(\d{5})-", href)
    code_postal = m_cp.group(1) if m_cp else ""

    ville_el = card.select_one('[itemprop="addressLocality"]')
    ville = ville_el.get_text(strip=True) if ville_el else ""

    # Type de bien lisible
    if _KEEP_TYPE.search(type_seg):
        m_type = _KEEP_TYPE.search(type_seg)
        type_bien = m_type.group(0).replace("-", " ").lower()
    else:
        type_bien = "maison"

    # Titre
    name_el = card.select_one('[itemprop="name"]')
    titre = name_el.get_text(" ", strip=True).strip('"') if name_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Description
    desc_el = card.select_one('[itemprop="description"]')
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Référence (id_annonce)
    ref = ""
    if link:
        m_ref = re.search(r"Bien\s+([A-Z0-9]+)", link.get_text(" ", strip=True))
        if m_ref:
            ref = m_ref.group(1)
    if not ref:
        m_uuid = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                           r"[0-9a-f]{4}-[0-9a-f]{12})", href)
        ref = m_uuid.group(1) if m_uuid else url
    id_annonce = ref

    # Prix
    price_el = card.select_one(".price span") or card.select_one(".price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Surface / pièces / chambres (.primaryinfo : "208 m² / 6 pièces / 5 chambres")
    info_el = card.select_one(".primaryinfo")
    info_text = info_el.get_text(" ", strip=True) if info_el else ""
    surface = _parse_int_unit(r"([\d\s\xa0]+)\s*m²", info_text, as_float=True)
    pieces = _parse_int_unit(r"(\d+)\s*pi[eè]ce", info_text)
    chambres = _parse_int_unit(r"(\d+)\s*chambre", info_text)

    # Surface terrain : non exposée dans la carte
    surface_terrain = None

    # Photo
    photos = []
    for img in card.select('img[itemprop="image"], img.media-object'):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "lesportecles",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
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
        "agence": "Les Portecles",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_int_unit(pattern: str, text: str, as_float: bool = False):
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        return float(val) if as_float else int(val)
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
    print(f"\nTotal Les Portecles: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

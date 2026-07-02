"""scrapers/imm_blois.py — Agence IMM (Blois, Loir-et-Cher 41)

Méthode : scrape_simple (httpx) — SSR HTML (CMS Immo-Facile / office12)
URL pattern : /annonces/transaction/Vente.html   (liste nationale = mono-agence 41)
              Le site est une agence locale : tout l'inventaire est en Loir-et-Cher (41)
              et alentours. Pas de filtre département serveur → on POST-FILTRE STRICT
              sur code_postal[:2] ∈ départements cibles (0 fuite hors-zone).

Cartes : div.product-listing
  - URL   : a[href*="/fiches/"]  → ../fiches/{cat}_{id}/{slug}.html (relatif → urljoin)
  - Titre : .products-name        → "Appartement 2 pièce(s) 56.39 m2"
  - Prix  : .products-price        → "124 200 €" ou "Vendu" (→ prix None)
  - Loc   : .products-localisation → "41000 BLOIS" ou "41260 LA CHAUSSEE SAINT VICTOR"
  - Desc  : .products-description
  - Photo : img.photo-listing[src] (relatif → urljoin)

Type de bien : déduit du titre / slug d'URL (maison, appartement, terrain…).
               On ne garde que maisons / propriétés (cf. le_tuc).
Surface / pièces : extraits du titre (« N pièce(s) », « NN.NN m2 »).

Pagination : la liste boucle (page=2 renvoie les mêmes cartes) → on déduplique
             par id_annonce et on s'arrête dès qu'une page n'apporte rien.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.imm-blois.com"
LIST_URL = "https://www.imm-blois.com/annonces/transaction/Vente.html"
MAX_PAGES = 6
PHOTOS_PER_CARD = 10


# Types de bien à conserver : maisons / propriétés / fermes...
# \b évite les faux positifs ("copropriete" ne doit pas matcher "propriete").
_KEEP_TYPE = re.compile(
    r"\b(?:maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme)\b",
    re.IGNORECASE,
)
# Types explicitement exclus (prioritaires sur _KEEP_TYPE)
_EXCLUDE_TYPE = re.compile(
    r"\b(?:appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"viager|cave|box|copropriete|copropriété|studio)\b",
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
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{LIST_URL}?manufacturers_id=transaction&page={page}&sort=0"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[ImmBlois] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.product-listing")
            if not cards:
                break

            # Détecte la fin de pagination indépendamment du filtrage métier :
            # on compte les cartes (par id de fiche) jamais vues sur les pages
            # précédentes. La liste boucle → quand 0 carte nouvelle, on s'arrête.
            page_ids = {_card_id(c) for c in cards}
            page_ids.discard(None)
            if not (page_ids - seen_ids):
                break

            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    cid = _card_id(card)
                    if cid:
                        seen_ids.add(cid)  # carte non-maison : marquée vue
                    continue

                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue

                # Post-filtre département STRICT (0 fuite hors-zone)
                cp = bien.get("code_postal") or ""
                if not cp or cp[:2] not in departements:
                    seen_ids.add(aid)  # vu mais hors-zone → ne pas recompter
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    seen_ids.add(aid)
                    continue
                if prix_min and p and p < prix_min:
                    seen_ids.add(aid)
                    continue
                if surface_min and s and s < surface_min:
                    seen_ids.add(aid)
                    continue

                seen_ids.add(aid)
                results.append(bien)

            await asyncio.sleep(0.6)

    print(f"[ImmBlois] {len(results)} annonces (zone cible)")
    return results


def _card_id(card) -> str | None:
    """Id numérique de la fiche (..._56937736/) — sert à la pagination/dédup."""
    link = card.select_one('a[href*="/fiches/"]')
    if not link:
        return None
    m = re.search(r"_(\d+)/", link.get("href", ""))
    return m.group(1) if m else None


def _parse_card(card) -> dict | None:
    link = card.select_one('a[href*="/fiches/"]')
    href = link.get("href", "") if link else ""
    if not href:
        return None
    # Liens relatifs depuis /annonces/transaction/ → URL absolue
    url = urljoin("https://www.imm-blois.com/annonces/transaction/", href)

    # id_annonce : segment numérique du dossier fiche (..._56937736/)
    id_annonce = None
    m_id = re.search(r"_(\d+)/", href)
    if m_id:
        id_annonce = m_id.group(1)
    if not id_annonce:
        id_annonce = url

    # Titre
    name_el = card.select_one(".products-name")
    titre = name_el.get_text(" ", strip=True) if name_el else ""
    titre = re.sub(r"\s+", " ", titre).strip()

    # Type de bien (titre + slug d'URL)
    slug = href.rsplit("/", 1)[-1]
    type_src = f"{titre} {slug}"
    # Exclusion prioritaire : si un type exclu apparaît, on rejette
    # (sauf si un type gardé apparaît AUSSI plus tôt — cas rare maison+garage,
    #  on tranche en faveur du type gardé seulement si exclu absent en début).
    if _EXCLUDE_TYPE.search(type_src):
        return None
    if not _KEEP_TYPE.search(type_src):
        return None
    type_bien = _type_from_text(type_src)

    # Localisation : "41000 BLOIS" / "41260 LA CHAUSSEE SAINT VICTOR"
    loc_el = card.select_one(".products-localisation")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    code_postal, ville = _parse_loc(loc)

    # Prix : "124 200 €" ou "Vendu"/"Nous consulter" → None
    price_el = card.select_one(".products-price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Description
    desc_el = card.select_one(".products-description")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Surface / pièces depuis le titre
    surface = _parse_surface(titre)
    pieces = _parse_pieces(titre)

    # Photos
    photos = []
    for img in card.select("img.photo-listing, .img-product img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(urljoin("https://www.imm-blois.com/annonces/transaction/", src))
    photos = photos[:PHOTOS_PER_CARD]

    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    return {
        "source": "imm_blois",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Agence IMM (Blois)",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _type_from_text(text: str) -> str:
    t = text.lower()
    for key, label in (
        ("chateau", "château"),
        ("château", "château"),
        ("manoir", "manoir"),
        ("longere", "longère"),
        ("longère", "longère"),
        ("ferme", "ferme"),
        ("propriete", "propriété"),
        ("propriété", "propriété"),
        ("villa", "villa"),
        ("maison", "maison"),
    ):
        if key in t:
            return label
    return "maison"


def _parse_loc(text: str) -> tuple[str, str]:
    """'41260 LA CHAUSSEE SAINT VICTOR' → ('41260', 'La Chaussee Saint Victor')"""
    m = re.search(r"\b(\d{5})\b", text)
    cp = m.group(1) if m else ""
    ville = re.sub(r"\b\d{5}\b", "", text).strip(" -,").strip()
    # Mise en capitalisation propre
    if ville.isupper():
        ville = ville.title()
    return cp, ville


def _parse_price(text: str) -> float | None:
    if not text:
        return None
    if "€" not in text and not re.search(r"\d{4,}", text):
        return None  # "Vendu", "Nous consulter", "Sous compromis"…
    cleaned = re.sub(r"[€\s\xa0 ]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        val = float(cleaned) if cleaned else None
    except ValueError:
        return None
    if val and val < 1000:  # garde-fou (m², pièces parasités)
        return None
    return val


def _parse_surface(text: str) -> float | None:
    """'... 56.39 m2' → 56.39"""
    m = re.search(r"(\d[\d\s\xa0]*[.,]?\d*)\s*m[²2]", text, re.IGNORECASE)
    if m:
        val = m.group(1).replace(" ", "").replace("\xa0", "").replace(",", ".")
        try:
            f = float(val)
            if 8 <= f <= 5000:
                return f
        except ValueError:
            pass
    return None


def _parse_pieces(text: str) -> int | None:
    """'Appartement 2 pièce(s) ...' / 'Maison T4' → 2 / 4"""
    m = re.search(r"(\d+)\s*pi[eè]ce", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"\bT\s*(\d+)\b", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
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
    print(f"\nTotal Imm Blois: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

"""scrapers/giboire.py — Groupe Giboire (réseau indépendant Grand Ouest / Bretagne)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress)
Site centenaire implanté à Rennes / Saint-Malo / Dinard (Ille-et-Vilaine, 35),
avec extension Pays de la Loire (Nantes 44, Maine-et-Loire 49, Vendée 85) et
Morbihan (56). Ne couvre PAS les départements du grand Val-de-Loire/Ouest
cibles du projet (72/28/45/89/37/36/18/58/41/53). Conservé pour le seul
chevauchement possible (49) et au cas où l'implantation s'étendrait.

URL pattern : /recherche-achat/{categorie}/[{ville-slug}/]
  - categorie : maison | appartement (on ignore terrain/stationnement/local)
  - PAS de slug par département : le filtre serveur n'accepte que des slugs de
    VILLE de sa zone (rennes, saint-malo, bruz…). Pour un département arbitraire,
    on scrape la recherche nationale de la catégorie et on POST-FILTRE strict
    sur code_postal[:2] → 0 fuite garantie.

Pagination : au-delà de la 1re page le site charge en AJAX/JS (les URLs
  ?page=N / /page/N/ renvoient 404 ou la même page). httpx ne voit donc que les
  ~12 premières cartes par catégorie. Suffisant ici : la zone cible n'a aucun
  stock Giboire (post-filtre = 0). Si Giboire s'implante dans la zone, brancher
  un slug ville ou l'endpoint AJAX.

Cartes : article.card[entityid]
  - URL    : a.card_title[href]   → /achat/{type}/{ville}/{slug}/  (ou /neuf/… pour le neuf)
  - Titre  : .card_title          → "Maison T7"
  - Loc    : .card_subtitle       → "RENNES ( 35200 )"
  - Prix   : .card_price          → "397 950 €"  ou  "A partir de 245 000 €"
  - Surf.  : texte de la carte     → "103 m2"
  - Pièces : "Tn" dans le titre
  - Chambres : texte de la carte   → "5 chambres"
  - Photos : img.img[src]

On ne garde que maisons / appartements (selon criteres["types"] si fourni).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.giboire.com"
PHOTOS_PER_CARD = 6


# Catégories de recherche scrapées (SSR). On exclut terrain/stationnement/local.
CATEGORIES = ["maison", "appartement"]

_EXCLUDE_TYPE = re.compile(
    r"terrain|stationnement|parking|garage|local|commerce|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)
    types = [t.lower() for t in criteres.get("types", []) if t]

    cats = CATEGORIES
    if types:
        cats = [c for c in CATEGORIES if any(c in t for t in types)] or CATEGORIES

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for cat in cats:
            url = f"{BASE_URL}/recherche-achat/{cat}/"
            try:
                cards = await _fetch_cards(client, url)
            except Exception as e:
                print(f"[Giboire] Erreur catégorie {cat}: {e}")
                continue

            kept = 0
            for card in cards:
                try:
                    bien = _parse_card(card, departements)
                except Exception:
                    continue
                if not bien:
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
                kept += 1

            print(
                f"[Giboire] Catégorie {cat}: {len(cards)} cartes, "
                f"{kept} retenues (dépts {departements})"
            )
            await asyncio.sleep(0.6)

    return results


async def _fetch_cards(client: httpx.AsyncClient, url: str) -> list:
    r = await client.get(url)
    if r.status_code != 200:
        return []
    return BeautifulSoup(r.text, "html.parser").select("article.card")


def _parse_card(card, departements: list[str]) -> dict | None:
    link = card.select_one("a.card_title") or card.select_one("a")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Filtre type via le segment d'URL (/achat/{type}/… ou /neuf/programme/…)
    path = re.sub(r"^https?://[^/]+", "", url)
    if _EXCLUDE_TYPE.search(path):
        return None

    # Localisation : "RENNES ( 35200 )"
    sub_el = card.select_one(".card_subtitle")
    loc = sub_el.get_text(" ", strip=True) if sub_el else ""
    ville, code_postal = _parse_loc(loc)

    # POST-FILTRE DÉPARTEMENT STRICT → 0 fuite hors-zone
    if not code_postal or code_postal[:2] not in departements:
        return None
    dept = code_postal[:2]

    # Titre : "Maison T7"
    title_el = card.select_one(".card_title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    # Type de bien depuis le segment d'URL (/achat/maison/… , /neuf/programme/…)
    type_bien = _type_from_path(path, titre)

    # Texte complet de la carte (surface / chambres)
    card_text = card.get_text(" ", strip=True)

    # Prix : "397 950 €" ou "A partir de 245 000 €"
    price_el = card.select_one(".card_price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Surface habitable : "103 m2"
    surface = _parse_surface(card_text)

    # Pièces : "Tn" dans le titre
    pieces = None
    m_t = re.search(r"\bT\s?(\d+)", titre, re.IGNORECASE)
    if m_t:
        pieces = int(m_t.group(1))

    # Chambres : "5 chambres"
    chambres = None
    m_ch = re.search(r"(\d+)\s*chambre", card_text, re.IGNORECASE)
    if m_ch:
        chambres = int(m_ch.group(1))

    # id_annonce : entityid de l'article, sinon ref du slug
    id_annonce = card.get("entityid") or ""
    if not id_annonce:
        slug = [p for p in path.split("/") if p]
        id_annonce = slug[-1] if slug else url

    # Photos
    photos = []
    for img in card.select("img.img, .card_image img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            src = src.replace("//app/", "/app/")
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "giboire",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": card_text[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Groupe Giboire",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'RENNES ( 35200 )' → ('RENNES', '35200')"""
    cp = ""
    m_cp = re.search(r"\(\s*(\d{5})\s*\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\(\s*\d{5}\s*\)\s*$", "", text).strip()
    return ville, cp


def _type_from_path(path: str, titre: str) -> str:
    parts = [p for p in path.split("/") if p]
    # /achat/{type}/{ville}/{slug}/  → parts[1] = type
    if len(parts) >= 2 and parts[0] in ("achat",):
        return parts[1].replace("-", " ")
    if "neuf" in parts:
        # programme neuf : déduire du titre si possible, sinon "programme neuf"
        if re.search(r"maison", titre, re.IGNORECASE):
            return "maison"
        if re.search(r"appartement", titre, re.IGNORECASE):
            return "appartement"
        return "programme neuf"
    m = re.search(r"\b(maison|appartement|villa)\b", titre, re.IGNORECASE)
    return m.group(1).lower() if m else "bien"


def _parse_surface(text: str) -> float | None:
    """'103 m2' → 103.0  (évite de confondre avec un prix mensuel)"""
    m = re.search(r"(\d[\d\s\xa0]*)\s*m[²2]\b", text, re.IGNORECASE)
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
                "types": getattr(criteres, "types_bien", []),
            }
        )
    )
    print(f"\nTotal Giboire: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p/{b['chambres'] or '?'}ch"
            f" — {b['type_bien']} — {b['ville']}"
        )

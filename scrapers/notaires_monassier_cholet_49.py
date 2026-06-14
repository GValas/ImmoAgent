"""scrapers/notaires_monassier_cholet_49.py — Groupe Monassier, étude de Cholet (49).

Méthode : scrape_simple (httpx) — SSR HTML (gabarit immonot/Realfusio « builder-equipe-
item » ; contenu dans le HTML brut, pas de JS).
Site : https://www.groupemonassier-cholet.notaires.fr
URL : /annonces-immobilieres/  (page liste unique, ~12 annonces, secteur Cholet 49 ;
      quelques biens limitrophes 44 → filtrage strict obligatoire). PAS de filtre serveur.
Cartes : div.builder-equipe-item
  - lien détail  → /annonce/vente-{type}-{ref}/
  - .immo-title  → « Maison 6 pièces 193.0 m² »
  - .immo-adress → « CHOLET (49) Puy-Saint-Bonnet » (ville + code département entre ())
  - .immo-price  → « 469 000 € »
Filtre DÉPARTEMENT : code dept extrait des parenthèses de .immo-adress → POST-FILTRE
  STRICT zone cible (rejette le 44 Nantes hors-zone) → 0 fuite. CP exact récupéré en
  page détail (gallery.py).

Interface : async def search(criteres: dict) -> list[dict]
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_int, parse_price

BASE_URL = "https://www.groupemonassier-cholet.notaires.fr"
LISTING_URL = f"{BASE_URL}/annonces-immobilieres/"
SOURCE = "notaires_monassier_cholet_49"

_KEEP_TYPE = re.compile(
    r"maison|villa|propri[ée]t[ée]|ferme|long[èe]re|manoir|chateau|ch[âa]teau|"
    r"moulin|demeure|domaine|mas",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|garage|parking|immeuble|local|commerce|bureau|fonds|"
    r"cave|box|studio|murs",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen: set[str] = set()

    async with make_client(timeout=25) as client:
        r = await get_with_retry(client, LISTING_URL)
        if r is None or r.status_code != 200:
            print(f"[NotairesMonassierCholet49] Listing inaccessible (status "
                  f"{getattr(r, 'status_code', 'None')})")
            return results

        cards = BeautifulSoup(r.text, "html.parser").select(".builder-equipe-item")
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue
            if bien["departement"] not in departements:
                continue
            aid = bien["id_annonce"]
            if aid in seen:
                continue
            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue
            seen.add(aid)
            results.append(bien)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print(f"[NotairesMonassierCholet49] total: {len(results)} biens — par dept: {by_dept}")
    return results


def _parse_card(card) -> dict | None:
    link = card.find("a", href=re.compile(r"/annonce/"))
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    title_el = card.select_one(".immo-title")
    title_txt = " ".join(title_el.get_text(" ", strip=True).split()) if title_el else ""

    type_part = title_txt.split(" ", 1)[0] if title_txt else ""
    if _EXCLUDE_TYPE.search(type_part) and not _KEEP_TYPE.search(type_part):
        return None
    if not _KEEP_TYPE.search(type_part):
        return None
    type_bien = type_part.lower()

    addr_el = card.select_one(".immo-adress")
    addr = addr_el.get_text(" ", strip=True) if addr_el else ""
    m_dep = re.search(r"\((\d{2,3})\)", addr)
    dept = m_dep.group(1)[:2] if m_dep else ""
    if not dept:
        return None
    # Ville : partie avant la parenthèse (titrée).
    ville = addr.split("(", 1)[0].strip().title()

    surface = None
    m_s = re.search(r"([\d]+(?:[.,]\d+)?)\s*m", title_txt)
    if m_s:
        try:
            surface = float(m_s.group(1).replace(",", "."))
            if not (8 <= surface <= 5000):
                surface = None
        except ValueError:
            surface = None
    pieces = parse_int(r"(\d+)\s*pi[èe]ce", title_txt)

    price_el = card.select_one(".immo-price")
    prix = None
    if price_el:
        prix = parse_price(re.sub(r"VENTE", "", price_el.get_text(" ", strip=True)))

    m_id = re.search(r"/annonce/[\w-]*?_([\w-]+)/?", href)
    id_annonce = m_id.group(1) if m_id else url

    photos: list[str] = []
    img = card.find("img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(src)

    titre = f"{type_bien.title()} à {ville}".strip() if ville else type_bien.title()

    return {
        "source": SOURCE,
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien[:60],
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos[:1],
        "dpe": None,
        "agence": "Groupe Monassier (Cholet)",
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "NotairesMonassierCholet49")

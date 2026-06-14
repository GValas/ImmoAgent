"""scrapers/notaires_gourlay_lemans_72.py — Office notarial Gourlay & Aveline,
Le Mans (72).

Méthode : scrape_simple (httpx) — SSR HTML (gabarit WordPress « article.property » ;
contenu dans le HTML brut, pas de JS).
Site : https://www.gourlay-aveline.notaires.fr
URL : /annonces-immobilieres/  (page liste unique, ~18 annonces, secteur Le Mans 72).
      PAS de filtre département serveur.
Cartes : article.property
  - data-url / lien → /annonce-immobiliere/vente-{type}-{P}-pieces-{S}-m2-a-{ville}[-sarthe]-{CP}/
    → le slug se termine par le CODE POSTAL COMPLET (72000…) → dept fiable.
  - .property-type (dernier span) → « Maison »
  - .property-price → « 357 000 € »
  - .property-size  → « 174.8 m² »
  - .property-nbrooms → « 7 pièces »
  - .property-location → ville « Rouillon »
Filtre DÉPARTEMENT : code_postal[:2] extrait du slug → POST-FILTRE STRICT zone cible →
  0 fuite hors-zone.

Interface : async def search(criteres: dict) -> list[dict]
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_int, parse_price

BASE_URL = "https://www.gourlay-aveline.notaires.fr"
LISTING_URL = f"{BASE_URL}/annonces-immobilieres/"
SOURCE = "notaires_gourlay_lemans_72"

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
            print(f"[NotairesGourlayLeMans72] Listing inaccessible (status "
                  f"{getattr(r, 'status_code', 'None')})")
            return results

        cards = BeautifulSoup(r.text, "html.parser").select("article.property")
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue
            cp = bien.get("code_postal") or ""
            if not cp or cp[:2] not in departements:
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
    print(f"[NotairesGourlayLeMans72] total: {len(results)} biens — par dept: {by_dept}")
    return results


def _parse_card(card) -> dict | None:
    href = card.get("data-url", "")
    if not href:
        a = card.find("a", href=True)
        href = a.get("href", "") if a else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    types = [t.get_text(strip=True) for t in card.select(".property-type")]
    type_txt = types[-1] if types else ""
    if _EXCLUDE_TYPE.search(type_txt) and not _KEEP_TYPE.search(type_txt):
        return None
    if not _KEEP_TYPE.search(type_txt):
        return None
    type_bien = type_txt.lower()

    # CP : dernier groupe de 5 chiffres du slug.
    m_cp = re.search(r"(\d{5})/?$", href.rstrip("/"))
    code_postal = m_cp.group(1) if m_cp else ""

    loc_el = card.select_one(".property-location")
    ville = loc_el.get_text(" ", strip=True).title() if loc_el else ""

    size_el = card.select_one(".property-size")
    surface = None
    if size_el:
        m_s = re.search(r"([\d.,]+)", size_el.get_text())
        if m_s:
            try:
                surface = float(m_s.group(1).replace(",", "."))
                if not (8 <= surface <= 5000):
                    surface = None
            except ValueError:
                surface = None

    rooms_el = card.select_one(".property-nbrooms")
    pieces = parse_int(r"(\d+)", rooms_el.get_text()) if rooms_el else None

    price_el = card.select_one(".property-price")
    prix = parse_price(price_el.get_text(" ", strip=True)) if price_el else None

    # id : id="property-NNNN" ou slug.
    pid = card.get("id", "")
    m_id = re.search(r"property-(\d+)", pid)
    id_annonce = m_id.group(1) if m_id else url

    photos: list[str] = []
    img_div = card.select_one(".property-img")
    if img_div and img_div.get("style"):
        m_u = re.search(r"url\(['\"]?([^'\")]+)", img_div["style"])
        if m_u:
            photos.append(m_u.group(1))

    titre = f"{type_bien.title()} à {ville}".strip() if ville else type_bien.title()

    return {
        "source": SOURCE,
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien[:60],
        "description": "",
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos[:1],
        "dpe": None,
        "agence": "Office notarial Gourlay & Aveline (Le Mans)",
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "NotairesGourlayLeMans72")

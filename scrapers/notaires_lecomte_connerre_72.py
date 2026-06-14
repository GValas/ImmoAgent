"""scrapers/notaires_lecomte_connerre_72.py — Office notarial Lecomte-Chérubin-Rivierre,
Connerré & Sargé-lès-le-Mans (72).

Méthode : scrape_simple (httpx) — SSR HTML (gabarit Realfusio « ns-property-card »
récent ; contenu dans le HTML brut, pas de JS).
Site : https://www.lecomte-cherubin-rivierre-connerre-sarge.notaires.fr
URL : /annonces-immobilieres/recherche.html  (page liste unique, ~24 annonces, secteur
      Est-manceau 72). PAS de filtre département serveur.
Cartes : div.ns-property-card
  - lien détail → /annonces-immobilieres/annonce/{ref}/{type}-a-vendre-{ville}-{CP}-{S}m2-{P}pieces.html
    → le slug encode TYPE, VILLE, CODE POSTAL COMPLET, surface habitable, pièces.
  - .c__location → « Le Mans - 72000 » (ville + CP, redondant avec le slug → fiable).
  - .c__price b  → prix « 209 000 € ».
  - bulles .qi__content : <strong> = surface habitable (fa-home), <b> fa-leaf = terrain,
    <b> = pièces. On privilégie le slug pour surface/pièces (ordre des bulles variable).
Filtre DÉPARTEMENT : code_postal[:2] (du slug, recoupé avec .c__location) → POST-FILTRE
  STRICT zone cible → 0 fuite hors-zone.

Interface : async def search(criteres: dict) -> list[dict]
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_price

BASE_URL = "https://www.lecomte-cherubin-rivierre-connerre-sarge.notaires.fr"
LISTING_URL = f"{BASE_URL}/annonces-immobilieres/recherche.html"
SOURCE = "notaires_lecomte_connerre_72"

_KEEP_TYPE = re.compile(
    r"maison|villa|propri[ée]t[ée]|ferme|long[èe]re|manoir|chateau|ch[âa]teau|"
    r"moulin|demeure|domaine|mas",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|garage|parking|immeuble|local|commerce|bureau|fonds|"
    r"cave|box|studio|murs|bois|etang|hangar",
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
            print(f"[NotairesLecomteConnerre72] Listing inaccessible (status "
                  f"{getattr(r, 'status_code', 'None')})")
            return results

        cards = BeautifulSoup(r.text, "html.parser").select(".ns-property-card")
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
    print(f"[NotairesLecomteConnerre72] total: {len(results)} biens — par dept: {by_dept}")
    return results


def _parse_card(card) -> dict | None:
    link = card.find("a", href=True)
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href
    slug = href.rsplit("/", 1)[-1].replace(".html", "")

    # Slug : « maison-a-vendre-le-mans-72000-94m2-6pieces »
    type_raw = slug.split("-a-vendre-", 1)[0] if "-a-vendre-" in slug else ""
    type_raw = type_raw.replace("-", " ").strip()
    if _EXCLUDE_TYPE.search(type_raw) and not _KEEP_TYPE.search(type_raw):
        return None
    if not _KEEP_TYPE.search(type_raw):
        return None
    type_bien = type_raw.lower()

    m_cp = re.search(r"-(\d{5})-", slug)
    code_postal = m_cp.group(1) if m_cp else ""
    # Repli/recoupement : .c__location « Le Mans - 72000 ».
    loc_el = card.select_one(".c__location")
    loc_txt = loc_el.get_text(" ", strip=True) if loc_el else ""
    if not code_postal:
        m_cp2 = re.search(r"(\d{5})", loc_txt)
        code_postal = m_cp2.group(1) if m_cp2 else ""
    ville = re.sub(r"\s*-?\s*\d{5}\s*$", "", loc_txt).strip().title()

    surface = None
    m_s = re.search(r"-(\d+)m2", slug)
    if m_s:
        try:
            surface = float(m_s.group(1))
            if not (8 <= surface <= 5000):
                surface = None
        except ValueError:
            surface = None
    pieces = None
    m_p = re.search(r"-(\d+)pieces?", slug)
    if m_p:
        pieces = int(m_p.group(1))

    price_el = card.select_one(".c__price b")
    prix = parse_price(price_el.get_text(" ", strip=True)) if price_el else None

    # id depuis le ref du href : /annonce/{REF}/...
    m_id = re.search(r"/annonce/([\w-]+)/", href)
    id_annonce = m_id.group(1) if m_id else url

    photos: list[str] = []
    img = card.find("img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:") and "marianne" not in src:
            photos.append(src if src.startswith("http") else BASE_URL + src)

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
        "agence": "Office notarial Lecomte-Chérubin-Rivierre (Connerré)",
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "NotairesLecomteConnerre72")

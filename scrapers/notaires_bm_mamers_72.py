"""scrapers/notaires_bm_mamers_72.py — Étude BM Notaires, Bonnétable & Mamers (72).

Méthode : scrape_simple (httpx) — SSR HTML (gabarit Realfusio « ns-property-card »,
contenu dans le HTML brut, pas de JS).
Site : https://www.bm-notaires-bonnetable-mamers.notaires.fr
URL : /annonces-immobilieres-sarthe.html  (page liste unique ; l'office est à cheval
      Sarthe 72 / Orne 61 → le listing mêle 72 et 61). PAS de filtre dept serveur.
Cartes : div.ns-property-card
  - .c__type span  → « Vente Maison »
  - .c__location   → ville « Bonnétable »
  - .c__price b    → prix « 87 330 € » (charge acquéreur / FAI)
  - lien détail    → /annonces/detail/{id}__{key}/key/N/vente-{type}-{dept-slug}-{ville}.html
                     → le NOM de département est dans le slug (sarthe / orne …).
  - .c__quickinfos → bulles : surface (m²), pièces, chambres (dans cet ordre).
Filtre DÉPARTEMENT : nom de dept extrait du slug → mappé en code → POST-FILTRE STRICT
  sur la zone cible (rejette « orne » 61 hors-zone) → 0 fuite. CP exact récupéré en page
  détail (gallery.py).

Interface : async def search(criteres: dict) -> list[dict]
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_price

BASE_URL = "https://www.bm-notaires-bonnetable-mamers.notaires.fr"
LISTING_URL = f"{BASE_URL}/annonces-immobilieres-sarthe.html"
SOURCE = "notaires_bm_mamers_72"

# Nom de département (slug) → code, pour les départements cibles uniquement.
DEPT_NAME_TO_CODE = {
    "sarthe": "72", "eure-et-loir": "28", "loiret": "45", "yonne": "89",
    "maine-et-loire": "49", "indre-et-loire": "37", "indre": "36", "cher": "18",
    "nievre": "58", "loir-et-cher": "41", "mayenne": "53",
}

_KEEP_TYPE = re.compile(
    r"maison|villa|propri[ée]t[ée]|ferme|long[èe]re|manoir|chateau|ch[âa]teau|"
    r"moulin|demeure|domaine|mas",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|garage|parking|immeuble|local|commerce|bureau|fonds|"
    r"cave|box|studio|murs|agricole",
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
            print(f"[NotairesBMMamers72] Listing inaccessible (status "
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
    print(f"[NotairesBMMamers72] total: {len(results)} biens — par dept: {by_dept}")
    return results


def _parse_card(card) -> dict | None:
    link = card.find("a", href=re.compile(r"/detail/"))
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href
    slug = href.rsplit("/", 1)[-1].replace(".html", "")

    # Département via nom dans le slug (le plus long matché d'abord).
    dept = ""
    for name in sorted(DEPT_NAME_TO_CODE, key=len, reverse=True):
        if f"-{name}-" in f"-{slug}-":
            dept = DEPT_NAME_TO_CODE[name]
            break
    if not dept:
        return None  # hors zone cible (ex. orne) → rejeté

    type_el = card.select_one(".c__type span") or card.select_one(".c__type")
    type_txt = type_el.get_text(" ", strip=True) if type_el else ""
    type_txt = re.sub(r"^\s*vente\s*", "", type_txt, flags=re.IGNORECASE).strip()
    if _EXCLUDE_TYPE.search(type_txt) and not _KEEP_TYPE.search(type_txt):
        return None
    if not _KEEP_TYPE.search(type_txt):
        return None
    type_bien = type_txt.lower()

    loc_el = card.select_one(".c__location")
    ville = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville = re.sub(r"\s*\(.*?\)\s*$", "", ville).strip()

    price_el = card.select_one(".c__price b")
    prix = parse_price(price_el.get_text(" ", strip=True)) if price_el else None

    # Bulles quickinfos : 1ʳᵉ valeur = surface (m²), 2ᵉ = pièces, 3ᵉ = chambres.
    bubbles = [b.get_text(strip=True) for b in card.select(".qi__content b")]
    surface = pieces = chambres = None
    if bubbles:
        try:
            surface = float(bubbles[0])
            if not (8 <= surface <= 5000):
                surface = None
        except ValueError:
            surface = None
        if len(bubbles) > 1 and bubbles[1].isdigit():
            pieces = int(bubbles[1])
        if len(bubbles) > 2 and bubbles[2].isdigit():
            chambres = int(bubbles[2])

    m_id = re.search(r"/detail/(\w+)", href)
    id_annonce = m_id.group(1) if m_id else url

    photos: list[str] = []
    media = card.select_one(".media")
    if media and media.get("style"):
        m_url = re.search(r"url\(([^)]+)\)", media["style"])
        if m_url:
            src = m_url.group(1).strip("'\"")
            if src and not src.startswith("data:"):
                photos.append(src if src.startswith("http") else BASE_URL + src)

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
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:1],
        "dpe": None,
        "agence": "Étude BM Notaires (Bonnétable / Mamers)",
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "NotairesBMMamers72")

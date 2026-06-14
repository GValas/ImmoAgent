"""scrapers/notaires_barre_saumur_49.py — Office notarial Barré, Malineau, Montanier
& Aubailly, Saumur (49).

Méthode : scrape_simple (httpx) — SSR HTML (gabarit UIkit « uk-card », contenu présent
dans le HTML brut, pas de JS).
Site : https://barre-malineau-montanier-saumur.notaires.fr
URL : /annonces-immobilieres/   (page liste unique, ~29 annonces, secteur saumurois 49,
      quelques biens limitrophes 37). PAS de filtre département serveur.
Cartes : a.uk-card
  - img alt/title → « VENTE TRADITIONNELLE - MAISON - 9 PIECES - 303 M2 - A SAUMUR
    (49400) » → on extrait TYPE, PIÈCES, SURFACE et le CODE POSTAL COMPLET (49400).
  - h3.el-title   → « Vente Maison »
  - .el-meta      → prix « 377 400 € »
  - .el-content   → ville « Saumur »
  - href          → /annonces-immobilieres?view=annonce&id=N
Filtre DÉPARTEMENT : POST-FILTRE STRICT code_postal[:2] ∈ departements cibles (le CP
  complet de l'alt image est fiable) → 0 fuite hors-zone vérifié (rejette le 37 si hors
  cible, ici 37 est cible donc conservé).

Interface : async def search(criteres: dict) -> list[dict]
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_int, parse_price

BASE_URL = "https://barre-malineau-montanier-saumur.notaires.fr"
LISTING_URL = f"{BASE_URL}/annonces-immobilieres/"
SOURCE = "notaires_barre_saumur_49"

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
            print(f"[NotairesBarreSaumur49] Listing inaccessible (status "
                  f"{getattr(r, 'status_code', 'None')})")
            return results

        cards = BeautifulSoup(r.text, "html.parser").select("a.uk-card")
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
    print(f"[NotairesBarreSaumur49] total: {len(results)} biens — par dept: {by_dept}")
    return results


def _parse_card(card) -> dict | None:
    href = card.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else f"{BASE_URL}{href}"

    img = card.find("img")
    alt = (img.get("alt") or img.get("title") or "") if img else ""

    h3 = card.select_one(".el-title")
    type_raw = h3.get_text(" ", strip=True) if h3 else ""
    type_raw = re.sub(r"^\s*vente\s*", "", type_raw, flags=re.IGNORECASE).strip()
    # L'alt image porte aussi le type ; on combine pour le filtrage.
    type_probe = f"{type_raw} {alt}"
    if _EXCLUDE_TYPE.search(type_probe) and not _KEEP_TYPE.search(type_raw):
        return None
    if not _KEEP_TYPE.search(type_raw):
        return None
    type_bien = (type_raw or "maison").lower()

    # CP complet depuis l'alt : « ... A SAUMUR (49400) ».
    m_cp = re.search(r"\((\d{5})\)", alt)
    code_postal = m_cp.group(1) if m_cp else ""

    cont = card.select_one(".el-content")
    ville = cont.get_text(" ", strip=True) if cont else ""

    surface = None
    m_s = re.search(r"(\d[\d\s]*)\s*M2", alt, re.IGNORECASE)
    if m_s:
        try:
            surface = float(re.sub(r"\s", "", m_s.group(1)))
        except ValueError:
            surface = None
    pieces = parse_int(r"(\d+)\s*PIECE", alt)

    meta = card.select_one(".el-meta")
    prix = parse_price(meta.get_text(" ", strip=True)) if meta else None

    m_id = re.search(r"id=(\d+)", href)
    id_annonce = m_id.group(1) if m_id else url

    photos: list[str] = []
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(src if src.startswith("http") else f"{BASE_URL}{src}")

    return {
        "source": SOURCE,
        "url": url,
        "id_annonce": id_annonce,
        "titre": (alt or f"{type_bien.title()} {ville}")[:150],
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
        "agence": "Office notarial Barré-Malineau-Montanier (Saumur)",
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "NotairesBarreSaumur49")

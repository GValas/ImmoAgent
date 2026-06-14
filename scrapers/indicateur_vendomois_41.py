"""scrapers/indicateur_vendomois_41.py — L'Indicateur Vendômois (Vendôme, 41)

Méthode : scrape_simple (httpx) — SSR HTML pur (thème LBI/Périmmo, même famille que
lesiteimmo). Agence indépendante locale (>50 ans) basée à Vendôme (Loir-et-Cher 41),
catalogue mono-zone Vendômois (Vendôme et communes alentour). Le code postal est dans
chaque carte → post-filtre STRICT par code_postal[:2] dans les départements cibles.

URL liste : https://www.indicateurvendomois.com/vente/{N}   (N = numéro de page, global,
            pas de paramètre département → mono-41 sécurisé par post-filtre).
Cartes : div.property-listing-v3__item.item
  - lien détail : /vente/41-loir-et-cher/{ville}/{type}/{tN}/{id-slug}/  (type+pièces dans l'URL)
  - ville+CP : .item__info-title  (« Vendôme (41100) »)
  - surface+prix : .item__info-extra  (« 107,92 m² », « 171 900 € »)
  - référence : .item__info-id  (« Réf : V70006810 »)
  - description : .item__container-text

Interface : async def search(criteres: dict) -> list[dict]
"""
from __future__ import annotations

import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import (
    get_with_retry,
    make_client,
    parse_loc,
    parse_price,
    standalone_main,
)

BASE_URL = "https://www.indicateurvendomois.com"
MAX_PAGES = 14
PHOTOS_PER_CARD = 8

_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|pavillon|fermette|corps-de-ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"autre|viager",
    re.IGNORECASE,
)


def _parse_surface_fr(text: str) -> float | None:
    """'107,92 m²' → 107.92 ; tolère espaces/insécables. None si rien."""
    m = re.search(r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m²", text or "")
    if not m:
        return None
    val = m.group(1).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        f = float(val)
        return f if 8 <= f <= 5000 else None
    except ValueError:
        return None


def _parse_card(card) -> dict | None:
    a = card.select_one("a[href]")
    href = a.get("href", "") if a else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # /vente/41-loir-et-cher/{ville}/{type}/{tN}/{id-slug}/
    parts = [p for p in href.split("/") if p]
    type_seg = ""
    pieces = None
    for p in parts:
        clean = re.sub(r"^\d+-", "", p)
        if _KEEP_TYPE.search(clean) or _EXCLUDE_TYPE.search(clean):
            type_seg = clean
        m_t = re.fullmatch(r"t(\d+)", p)
        if m_t:
            pieces = int(m_t.group(1))
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if type_seg and not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = (type_seg or "maison").replace("-", " ")

    # Référence (id_annonce)
    ref_el = card.select_one(".item__info-id")
    ref = ""
    if ref_el:
        m_ref = re.search(r"Réf\s*:?\s*([A-Za-z0-9]+)", ref_el.get_text(" ", strip=True))
        ref = m_ref.group(1) if m_ref else ""
    id_annonce = ref or url

    title_el = card.select_one(".item__info-title")
    ville, code_postal = parse_loc(title_el.get_text(" ", strip=True) if title_el else "")

    # Surface + prix dans les .item__info-extra
    extras = " ".join(e.get_text(" ", strip=True) for e in card.select(".item__info-extra"))
    surface = _parse_surface_fr(extras)
    prix = parse_price(re.search(r"([\d\s\xa0]+)\s*€", extras).group(1)) if "€" in extras else None

    desc_el = card.select_one(".item__container-text")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    titre = (a.get("title") or "").replace("Voir le bien - ", "").strip()
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    photos = []
    for img in card.select("img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "indicateur_vendomois_41",
        "url": url,
        "id_annonce": str(id_annonce),
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
        "agence": "L'Indicateur Vendômois",
    }


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen: set[str] = set()

    async with make_client() as client:
        for page in range(1, MAX_PAGES + 1):
            r = await get_with_retry(client, f"{BASE_URL}/vente/{page}")
            if r is None or r.status_code != 200:
                break
            cards = BeautifulSoup(r.text, "html.parser").select(
                ".property-listing-v3__item.item"
            )
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
                cp = bien.get("code_postal") or ""
                if not cp or cp[:2] not in departements:
                    continue
                if bien["id_annonce"] in seen:
                    continue
                seen.add(bien["id_annonce"])
                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue
                results.append(bien)
                new_on_page += 1

            if new_on_page == 0 and not cards:
                break
            await asyncio.sleep(0.5)

    print(f"[IndicVendomois] {len(results)} annonces (depts {sorted(departements)})")
    return results


if __name__ == "__main__":
    standalone_main(search, "IndicateurVendomois")

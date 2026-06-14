"""scrapers/nemrod_immo_41.py — NEMROD Immobilier Sologne (Romorantin, 41)

Méthode : scrape_simple (httpx) — SSR HTML pur (thème Zenia/Apimo).
Agence indépendante mono-zone Sologne (cœur Loir-et-Cher 41 : Romorantin-Lanthenay,
Pruniers, Selles-sur-Cher, Gièvres, Mur-de-Sologne… + frange 18/45). Le site héberge
aussi un pôle Paris : on ne scrape QUE la liste Sologne (`/fr/nemrod-sologne-ventes`),
puis post-filtre STRICT par code_postal[:2] dans les départements cibles.

URL liste : https://nemrod-immo.fr/fr/nemrod-sologne-ventes?page=N
Cartes : li.property (data-property-id), lien /fr/propriete/vente+{type}+{ville}+{slug}+{id}
Le code postal n'est PAS exposé dans le HTML (l'adresse affichée est celle de l'agence) :
on le résout depuis le slug ville de l'URL via geo.api.gouv.fr (cache mémoire), en
privilégiant la commune située dans un département cible si le nom est ambigu (Billy…).

Interface : async def search(criteres: dict) -> list[dict]
"""
from __future__ import annotations

import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import (
    get_with_retry,
    make_client,
    parse_int,
    parse_price,
    parse_surface,
    standalone_main,
)

BASE_URL = "https://nemrod-immo.fr"
LIST_URL = BASE_URL + "/fr/nemrod-sologne-ventes"
MAX_PAGES = 6
PHOTOS_PER_CARD = 8

# Départements cibles (post-filtre strict).
TARGET_DEPTS = {"72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"}

_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|pavillon|corps-de-ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|viager",
    re.IGNORECASE,
)

# Cache slug ville -> (code_postal, departement) pour éviter de re-questionner l'API geo.
_CP_CACHE: dict[str, tuple[str, str]] = {}


async def _resolve_cp(client, ville_slug: str) -> tuple[str, str]:
    """Résout un slug ville Sologne -> (code_postal, dept) via geo.api.gouv.fr.

    Privilégie une commune dans un département cible si le nom est ambigu
    (ex. « Billy » existe en 03 ET 41 → on garde 41). Retourne ("","") si échec.
    """
    if ville_slug in _CP_CACHE:
        return _CP_CACHE[ville_slug]
    nom = ville_slug.replace("-", " ").strip()
    res = ("", "")
    try:
        r = await client.get(
            "https://geo.api.gouv.fr/communes",
            params={"nom": nom, "fields": "codesPostaux,codeDepartement", "limit": 5},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            # 1) priorité à une commune en département cible
            chosen = next(
                (c for c in data if c.get("codeDepartement") in TARGET_DEPTS), None
            )
            chosen = chosen or (data[0] if data else None)
            if chosen and chosen.get("codesPostaux"):
                res = (chosen["codesPostaux"][0], chosen.get("codeDepartement", ""))
    except Exception:
        res = ("", "")
    _CP_CACHE[ville_slug] = res
    return res


def _parse_card(card) -> dict | None:
    a = card.select_one("a[href]")
    href = a.get("href", "") if a else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # /fr/propriete/vente+{type}+{ville-slug}+{libelle}+{id}
    m = re.match(r"/fr/propriete/vente\+(\w+)\+([a-z0-9\-]+)\+", href)
    type_seg = (m.group(1) if m else "").lower()
    ville_slug = m.group(2) if m else ""
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if type_seg and not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg or "maison"

    id_annonce = card.get("data-property-id") or ""
    if not id_annonce:
        mid = re.search(r"\+(\d+)$", href)
        id_annonce = mid.group(1) if mid else url

    h3 = card.select_one("h3")
    titre = h3.get_text(" ", strip=True) if h3 else ""
    h2 = card.select_one("h2")
    ville_aff = h2.get_text(" ", strip=True) if h2 else ""
    if not titre:
        titre = f"{type_bien.title()} {ville_aff}".strip()

    price_el = card.select_one(".price")
    prix = parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    rooms_el = card.select_one("li.rooms span")
    pieces = parse_int(r"(\d+)", rooms_el.get_text(strip=True)) if rooms_el else None
    bed_el = card.select_one("li.bedrooms span")
    chambres = parse_int(r"(\d+)", bed_el.get_text(strip=True)) if bed_el else None
    area_el = card.select_one("li.area span")
    surface = None
    if area_el:
        m_area = re.search(r"(\d[\d\s\xa0]*)", area_el.get_text(strip=True))
        if m_area:
            surface = parse_surface(m_area.group(1) + " m² hab")

    photos = []
    for img in card.select("img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "nemrod_immo_41",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": "",
        "ville": (ville_aff or ville_slug.replace("-", " ").title())[:80],
        "code_postal": "",
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "NEMROD Immobilier Sologne",
        "_ville_slug": ville_slug,
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
            url = f"{LIST_URL}?page={page}"
            r = await get_with_retry(client, url)
            if r is None or r.status_code != 200:
                break
            cards = BeautifulSoup(r.text, "html.parser").select("li.property")
            if not cards:
                break

            page_biens: list[dict] = []
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue
                if bien["id_annonce"] in seen:
                    continue
                seen.add(bien["id_annonce"])
                page_biens.append(bien)

            if not page_biens:
                break

            # Résolution du code postal (geo API) puis filtre dept strict.
            for bien in page_biens:
                slug = bien.pop("_ville_slug", "")
                cp, dep = await _resolve_cp(client, slug)
                bien["code_postal"] = cp
                bien["departement"] = dep
                await asyncio.sleep(0.2)

            for bien in page_biens:
                cp = bien.get("code_postal") or ""
                dep = cp[:2]
                if not cp or dep not in departements:
                    continue
                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue
                results.append(bien)

            await asyncio.sleep(0.5)

    print(f"[Nemrod] {len(results)} annonces (depts {sorted(departements)})")
    return results


if __name__ == "__main__":
    standalone_main(search, "Nemrod")

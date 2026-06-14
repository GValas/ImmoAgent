"""scrapers/laplacette.py — Agence La Placette (Angers, Maine-et-Loire 49)

Méthode : scrape_simple (httpx) — SSR HTML.
Agence indépendante familiale d'Angers (depuis 1960), biens d'Anjou (49 :
Angers, Les Ponts-de-Cé, Beaufort-en-Anjou…). Petit portail (~10 biens).

URL pattern : /fr/ventes?page=N   (listing GLOBAL, pas de slug département)
Cartes : li.property[data-property-id]
  - ville  : h3
  - type+prix : p  (« Maison », span.price)
  - surface : ul li.area  (« 136 m² »)
  - photo   : div.picture img
  - lien    : a[href] → /fr/propriété/{id}

La carte n'expose PAS le code postal → on résout ville→département via
l'API officielle geo.api.gouv.fr (cache en mémoire) puis POST-FILTRE strict
sur le département cible (0 fuite). Types non habitat (local, fonds, immeuble,
terrain, ensemble pro) écartés.

Interface : async def search(criteres: dict) -> list[dict]
"""
from __future__ import annotations

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import (
    get_with_retry,
    make_client,
    parse_price,
    parse_surface,
    standalone_main,
)

BASE_URL = "https://www.agencelaplacette.com"
SOURCE = "laplacette"
LABEL = "LaPlacette"
MAX_PAGES = 6
GEO_API = "https://geo.api.gouv.fr/communes"

_EXCLUDE_TYPE = re.compile(
    r"local|fonds|commerc|immeuble|terrain|parking|garage|bureau|"
    r"ensemble immobilier|professionnel",
    re.IGNORECASE,
)
_TYPE_MAP = [
    (re.compile(r"appartement|studio", re.I), "appartement"),
    (re.compile(r"maison|villa|propri|demeure|manoir|ch[aâ]teau|longere|longère|"
                r"ferme|moulin", re.I), "maison"),
]

# Cache ville (normalisée) → code département (évite de re-requêter geo.api).
_DEPT_CACHE: dict[str, tuple[str | None, str | None]] = {}


def _detect_type(label: str) -> str | None:
    if _EXCLUDE_TYPE.search(label):
        return None
    for rx, t in _TYPE_MAP:
        if rx.search(label):
            return t
    return None  # type inconnu → exclu par prudence


async def _resolve_dept(client: httpx.AsyncClient, ville: str) -> tuple[str | None, str | None]:
    """(code_departement, code_postal) pour `ville` via geo.api.gouv.fr ; (None, None) si échec."""
    key = ville.strip().lower()
    if not key:
        return None, None
    if key in _DEPT_CACHE:
        return _DEPT_CACHE[key]
    res: tuple[str | None, str | None] = (None, None)
    try:
        r = await client.get(
            GEO_API,
            params={"nom": ville, "fields": "codeDepartement,codesPostaux",
                    "boost": "population", "limit": 1},
            timeout=12,
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                dep = data[0].get("codeDepartement")
                cps = data[0].get("codesPostaux") or []
                cp = cps[0] if cps else None
                res = (dep, cp)
    except Exception:
        res = (None, None)
    _DEPT_CACHE[key] = res
    return res


def _parse_card(card) -> dict | None:
    a = card.select_one("a[href]")
    if not a:
        return None
    href = a["href"]
    url = href if href.startswith("http") else BASE_URL + href

    pid = card.get("data-property-id") or href.rstrip("/").split("/")[-1]

    h3 = card.select_one("h3")
    ville = h3.get_text(" ", strip=True) if h3 else ""

    p = card.select_one("p")
    p_text = p.get_text(" ", strip=True) if p else ""
    type_bien = _detect_type(p_text)
    if type_bien is None:
        return None

    price_el = card.select_one(".price")
    prix = parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    area_el = card.select_one(".area")
    surface = None
    if area_el:
        m = re.search(r"([\d\s\xa0.,]+)\s*m", area_el.get_text(" ", strip=True))
        if m:
            val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
            try:
                surface = float(val)
            except ValueError:
                surface = None
    if surface is None:
        surface = parse_surface(p_text)

    img = card.select_one(".picture img")
    photos = []
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)

    titre = (img.get("alt").strip() if img and img.get("alt") else "") or \
            f"{type_bien.title()} {ville}".strip()

    return {
        "source": SOURCE,
        "url": url,
        "id_annonce": str(pid),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": None,   # rempli après résolution geo.api
        "ville": ville[:80],
        "code_postal": None,
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Agence La Placette",
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
            r = await get_with_retry(client, f"{BASE_URL}/fr/ventes?page={page}")
            if r is None or r.status_code != 200:
                break
            cards = BeautifulSoup(r.text, "html.parser").select("li.property")
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
                aid = bien["id_annonce"]
                if aid in seen:
                    continue

                dep, cp = await _resolve_dept(client, bien["ville"])
                if dep is None or dep not in departements:
                    continue  # POST-FILTRE dept STRICT (0 fuite)
                bien["departement"] = dep
                bien["code_postal"] = cp

                pr = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and pr and pr > prix_max:
                    continue
                if prix_min and pr and pr < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                seen.add(aid)
                results.append(bien)
                new_on_page += 1

            print(f"[{LABEL}] Page {page}: {len(cards)} cartes, {new_on_page} retenues")
            if new_on_page == 0 and len(cards) < 9:
                break
            await asyncio.sleep(0.5)

    print(f"[{LABEL}] Total : {len(results)} annonces")
    return results


if __name__ == "__main__":
    standalone_main(search, "Agence La Placette")

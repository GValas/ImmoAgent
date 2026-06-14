"""scrapers/lieux_uniques.py — Lieux Uniques® (immobilier de charme / prestige Val de Loire)

Méthode : scrape_simple (httpx) — SSR HTML.

Réseau « propriétés de charme & vieilles pierres » implanté Blois / Chambord /
Tours / Orléans / Bourges (Val de Loire + Berry), mais qui diffuse aussi des biens
« France et Étranger » (44, 35, 17, 59, 14, 40…). Il N'Y A PAS de filtre département
côté serveur (liste nationale unique paginée) → POST-FILTRE STRICT sur code_postal[:2]
impératif pour ne garder que les départements cibles (37, 41, 45, 18, 36…).

URL liste : /fr/vente-propriete-villas-france-etranger/[p=N]   (12 cartes/page,
            pagination p=2..N ; la liste « boucle » au-delà de la dernière page →
            on s'arrête dès qu'une référence déjà vue réapparaît).
Cartes : article.annonce_listing
  - URL/ref  : a[href*='ref-li...'] → .../ref-{ref}/{type}-{pieces}-pieces-...-{ville}-{CP}/
  - Type     : .type
  - Lieu     : .address          → "BLOIS (41000)"
  - Prix     : .price            → "405 000 €"
  - Détails  : .infoSup          → "8 pièces - 6 chambres - 212 m²"
  - Photo    : img[src*='/datas/biens/']

Surface terrain / DPE / description : absents des cartes → laissés à None (enrichis
en page détail par scrapers/gallery.py côté pipeline).

Interface : async def search(criteres: dict) -> list[dict]
"""
from __future__ import annotations

import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_int, parse_price

BASE_URL = "https://www.lieuxuniques.com"
LIST_PATH = "/fr/vente-propriete-villas-france-etranger/"
PHOTOS_PER_CARD = 8
MAX_PAGES = 8

_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_refs: set[str] = set()
    leaked = 0

    async with make_client(timeout=25) as client:
        for page in range(1, MAX_PAGES + 1):
            url = BASE_URL + LIST_PATH + ("" if page == 1 else f"p={page}")
            r = await get_with_retry(client, url)
            if r is None or r.status_code != 200:
                break
            cards = BeautifulSoup(r.text, "html.parser").select("article.annonce_listing")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                bien = _parse_card(card)
                if not bien:
                    continue
                ref = bien["id_annonce"]
                if ref in seen_refs:
                    continue
                seen_refs.add(ref)
                new_on_page += 1

                cp = bien["code_postal"]
                # POST-FILTRE DÉPARTEMENT STRICT (site = liste nationale)
                if not cp or cp[:2] not in departements:
                    leaked += 1
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

            # La liste reboucle après la dernière page (page N+1 == page 1) :
            # si la page n'apporte AUCUNE nouvelle référence, on s'arrête.
            if new_on_page == 0:
                break
            await asyncio.sleep(0.5)

    print(f"[LieuxUniques] {len(results)} annonces (dept cibles) — {leaked} hors-zone écartées")
    return results


def _parse_card(card) -> dict | None:
    link = card.find("a", href=re.compile(r"ref-li"))
    if not link:
        return None
    href = link.get("href", "")
    url = href if href.startswith("http") else BASE_URL + href

    m_ref = re.search(r"ref-([a-z0-9\-]+?)/", href, re.IGNORECASE)
    id_annonce = m_ref.group(1) if m_ref else url

    # CP en fin de slug : .../...-ville-41000/
    m_cp = re.search(r"-(\d{5})/?$", href.rstrip("/") + "/")
    code_postal = m_cp.group(1) if m_cp else ""

    addr_el = card.select_one(".address")
    addr_txt = addr_el.get_text(" ", strip=True) if addr_el else ""
    if not code_postal:
        m = re.search(r"\((\d{5})\)", addr_txt)
        code_postal = m.group(1) if m else ""
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", addr_txt).strip().title()

    type_el = card.select_one(".type")
    type_bien = (type_el.get_text(" ", strip=True) if type_el else "").lower()
    if _EXCLUDE_TYPE.search(type_bien) and not _KEEP_TYPE.search(type_bien):
        return None
    if not type_bien:
        type_bien = "maison"

    price_el = card.select_one(".price")
    prix = parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    info_el = card.select_one(".infoSup")
    info_txt = info_el.get_text(" ", strip=True) if info_el else ""
    pieces = parse_int(r"(\d+)\s*pi[eè]ces?", info_txt)
    chambres = parse_int(r"(\d+)\s*chambres?", info_txt)
    surface = None
    m_s = re.search(r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m²", info_txt)
    if m_s:
        try:
            surface = float(re.sub(r"[\s\xa0]", "", m_s.group(1)).replace(",", "."))
        except ValueError:
            surface = None

    titre = f"{type_bien.title()} {ville}".strip()

    photos = []
    for img in card.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original") or ""
        if "/datas/biens/" in src and not src.startswith("data:"):
            full = src if src.startswith("http") else BASE_URL + src
            if full not in photos:
                photos.append(full)

    return {
        "source": "lieux_uniques",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Lieux Uniques",
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main

    standalone_main(search, "Lieux Uniques")

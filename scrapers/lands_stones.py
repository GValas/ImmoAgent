"""scrapers/lands_stones.py — Lands & Stones (haras / fermes / propriétés équestres)

Site : https://landsandstones.com — agence spécialisée dans les haras, fermes
       d'élevage et propriétés équestres (catalogue de niche, surtout Normandie).

Méthode : scrape_simple (httpx) — SSR HTML pur (le HTML brut contient titres,
          dept, surface, prix : pas de CSR, vérifié).

URL de listing : /real_estate/?l={offset}
  Le paramètre `l` est un OFFSET SQL (LIMIT {l}, 10 — fuité dans un data-q du HTML) :
  on pagine l=0, 10, 20… jusqu'à page vide. Catalogue très réduit (~19 biens).

Cartes (liste) : div.bien
  - URL    : a[href*="real_estate/detail"]
  - Titre  : a.fancybox[title]
  - Loc    : p.pbieninfo > span  → "Orne (61) - 63 Ha"  (région + dept + ha)
  - Desc   : p.pbiendesc          → "1882 - HARAS 63 HA ... <description>"
  - Prix   : p.pnewbienprice      → "Prix : Nous consulter" (souvent sur demande)
  - Photo  : a.fancybox[href] / img[src]

Le site n'expose PAS de code postal à 5 chiffres en liste — seulement le numéro
de département entre parenthèses « (61) ». On construit donc `code_postal` = ce
numéro de dept (2 chiffres) ; le POST-FILTRE strict code_postal[:2] ∈ cibles
fonctionne tel quel (0 fuite). Spécialiste haras Normandie → 0 stock attendu en
zone cible, mais le scraper reste fonctionnel.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS, parse_price

BASE_URL = "https://landsandstones.com"
LIST_URL = BASE_URL + "/real_estate/?l={offset}"
PAGE_STEP = 10          # LIMIT {offset}, 10
MAX_OFFSET = 300        # garde-fou (catalogue ~19 biens → s'arrête bien avant)
PHOTOS_PER_CARD = 10


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        offset = 0
        while offset <= MAX_OFFSET:
            url = LIST_URL.format(offset=offset)
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[LandsStones] Erreur offset {offset}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.bien")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                bien = _parse_card(card)
                if not bien:
                    continue
                if bien["id_annonce"] in seen_ids:
                    continue
                new_on_page += 1
                seen_ids.add(bien["id_annonce"])

                cp = bien["code_postal"]
                if not cp or cp[:2] not in departements:
                    continue
                p = bien.get("prix") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                s = bien.get("surface") or 0
                if surface_min and s and s < surface_min:
                    continue
                results.append(bien)

            if new_on_page == 0:
                break
            offset += PAGE_STEP
            await asyncio.sleep(0.5)

    print(f"[LandsStones] {len(results)} biens retenus en zone cible "
          f"(catalogue haras, surtout Normandie)")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one('a[href*="real_estate/detail"]')
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce : id du chemin /detail/{id}/...  (secours : id="bienNNNN")
    m_id = re.search(r"/detail/(\d+)/", href)
    id_annonce = m_id.group(1) if m_id else ""
    if not id_annonce:
        card_id = card.get("id", "")
        m2 = re.search(r"(\d+)", card_id)
        id_annonce = m2.group(1) if m2 else url
    id_annonce = str(id_annonce)

    fancy = card.select_one("a.fancybox[title]")
    titre = (fancy.get("title") or "").strip() if fancy else ""

    info_el = card.select_one("p.pbieninfo")
    info_txt = info_el.get_text(" ", strip=True) if info_el else ""
    region = ""
    spans = info_el.select("span") if info_el else []
    if spans:
        region = spans[0].get_text(strip=True)

    # Département entre parenthèses : "Orne (61) - 63 Ha"
    dept = ""
    m_dep = re.search(r"\((\d{2,3})\)", info_txt)
    if m_dep:
        dept = m_dep.group(1).zfill(2)
    # Pas de commune en liste : on garde le NOM du département comme "ville"
    # approximative — soit le 2e span ("Orne (61) - 63 Ha"), soit le mot juste
    # avant la parenthèse (en retirant le préfixe région éventuel).
    ville = ""
    dep_span = spans[1].get_text(" ", strip=True) if len(spans) > 1 else info_txt
    m_ville = re.search(r"([A-Za-zÀ-ÿ'-]+(?:[ -][A-Za-zÀ-ÿ'-]+)?)\s*\(\d{2,3}\)", dep_span)
    if m_ville:
        ville = m_ville.group(1).strip()

    # Surface terrain en hectares → m²
    surface_terrain = None
    m_ha = re.search(r"([\d,.]+)\s*[Hh]a\b", info_txt)
    if m_ha:
        try:
            surface_terrain = float(m_ha.group(1).replace(",", ".")) * 10000
        except ValueError:
            surface_terrain = None

    desc_el = card.select_one("p.pbiendesc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Surface habitable éventuelle dans la description ("250 m2")
    surface = None
    m_s = re.search(r"(\d[\d\s]*)\s*m[²2]\b", description)
    if m_s:
        try:
            surface = float(re.sub(r"\s", "", m_s.group(1)))
        except ValueError:
            surface = None

    # Prix : soit "Nous consulter", soit "honoraires inclus : 2 520 000 Euros …"
    # (plusieurs montants → on prend le PREMIER = honoraires inclus / prix FAI).
    price_el = card.select_one("p.pnewbienprice")
    price_txt = price_el.get_text(" ", strip=True) if price_el else ""
    prix = None
    m_p = re.search(r"([\d][\d\s\xa0]{4,})\s*(?:€|Euros?)", price_txt)
    if m_p:
        prix = parse_price(m_p.group(1))

    if not titre:
        titre = description[:120] or f"Propriété équestre {ville}".strip()

    photos = []
    if fancy and fancy.get("href"):
        photos.append(fancy["href"])
    for img in card.select("img"):
        src = img.get("src") or ""
        if src and "uploads/biens" in src and src not in photos:
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "lands_stones",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "propriété équestre",
        "description": description[:1200],
        "departement": dept,
        "ville": (ville or region)[:80],
        "code_postal": dept,          # pas de CP 5 chiffres en liste → dept (2 chiffres)
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Lands & Stones",
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main

    standalone_main(search, "Lands & Stones")

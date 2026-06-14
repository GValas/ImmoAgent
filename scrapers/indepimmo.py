"""scrapers/indepimmo.py — Indep'Immo (agence indépendante, Cholet & St-Macaire, 49/44)

Méthode : scrape_simple (httpx) — SSR HTML
Site : https://www.indepimmo.fr
URL pattern : /vente/            (page 1)
              /vente/page-{N}.html  (pages suivantes)
              → listing UNIQUE non filtrable par département côté serveur (l'agence
                opère Cholet/Mauges à cheval sur 49 et 44 Nantes). Filtre département
                OBLIGATOIREMENT côté client.
Cartes : div.short_product
  - lien/url   : a.LinkIn[href]
  - prix       : .info_prix .prix      (« 157500 € »)
  - photos     : .photos (compteur) + img src
  - titre/CP   : h2.titre a → « maison - 4 chambre(s) - 104 m² ... TREMENTINES (49340) »
                 → on extrait le code postal complet entre () → filtre dept STRICT.
  - référence  : .ref
  - description: .description
Filtre département : post-filtre STRICT code_postal[:2] ∈ departements cibles
  (la liste mélange 49 et 44) → 0 fuite hors-zone vérifié.

Interface : async def search(criteres: dict) -> list[dict]
"""
from __future__ import annotations

import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_int, parse_price

BASE_URL = "https://www.indepimmo.fr"
MAX_PAGES = 8

_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|longere|longère|manoir|chateau|château|demeure|ferme|"
    r"corps de ferme|moulin|villa|gite|gîte",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|appart|studio|terrain|parking|box|local|bureau|immeuble",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen: set[str] = set()

    async with make_client() as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/vente/" if page == 1 else f"{BASE_URL}/vente/page-{page}.html"
            r = await get_with_retry(client, url)
            if r is None or r.status_code != 200:
                break
            cards = BeautifulSoup(r.text, "html.parser").select("div.short_product")
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
                # Garde-fou département STRICT (liste mêle 49 et 44).
                if not cp or cp[:2] not in departements:
                    continue
                aid = bien.get("id_annonce") or bien.get("url")
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
                new_on_page += 1

            if new_on_page == 0 and page > 1:
                break
            await asyncio.sleep(0.5)

    depts_vus = sorted({b["code_postal"][:2] for b in results})
    print(f"[IndepImmo] {len(results)} annonces (depts {depts_vus})")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a.LinkIn[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else f"{BASE_URL}{href}"

    title_el = card.select_one("h2.titre a")
    full_txt = title_el.get_text(" ", strip=True) if title_el else ""
    if not full_txt:
        return None

    # Type : on ne garde que maison/propriété/longère…
    if _EXCLUDE_TYPE.search(full_txt) and not _KEEP_TYPE.search(full_txt):
        return None
    if not _KEEP_TYPE.search(full_txt):
        return None

    # CP + ville depuis le .small : « TREMENTINES (49340) ».
    small_el = card.select_one("h2.titre .small")
    small = small_el.get_text(" ", strip=True) if small_el else ""
    m_cp = re.search(r"\((\d{5})\)", small)
    code_postal = m_cp.group(1) if m_cp else ""
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", small).strip().title()

    chambres = parse_int(r"(\d+)\s*chambre", full_txt)
    surface = None
    m_s = re.search(r"(\d[\d\s]*)\s*m²", full_txt)
    if m_s:
        try:
            surface = float(re.sub(r"\s", "", m_s.group(1)))
        except ValueError:
            surface = None

    # id_annonce : numéro dans le slug /vente/{ID}-...
    m_id = re.search(r"/vente/(\d+)-", href)
    ref_el = card.select_one(".ref")
    ref = ref_el.get_text(strip=True) if ref_el else ""
    id_annonce = (m_id.group(1) if m_id else "") or ref or url

    price_el = card.select_one(".info_prix .prix")
    prix = parse_price(price_el.get_text(" ", strip=True)) if price_el else None

    desc_el = card.select_one(".description")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    photos: list[str] = []
    img = card.select_one(".picture img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if not src.startswith("http"):
                src = f"{BASE_URL}{src}"
            photos.append(src)

    return {
        "source": "indepimmo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": full_txt[:150],
        "type_bien": "maison",
        "description": description[:1200],
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Indep'Immo",
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Indep'Immo")

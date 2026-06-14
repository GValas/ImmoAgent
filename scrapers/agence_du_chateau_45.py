"""scrapers/agence_du_chateau_45.py — Agence du Château (Sully-sur-Loire, Loiret 45)

Méthode : scrape_simple (httpx) — SSR HTML (CMS estate_i18n).
Agence indépendante de Sully-sur-Loire (45), secteur Val de Sully / Sologne du
Loiret (déborde sur le 41, cible — ex. Neung-sur-Beuvron).

URL liste maisons : /immobilier/type/maison?page=N   (9 biens/page)
Cartes : div.bienAccueil
  - titre  : .titreAnnonce
  - prix   : .prixCoeur strong        « 27 500 € » (les locations sont en « €/mois »)
  - loc    : .localisation            « Lion-en-Sullias (45600) » → ville + CP PRÉSENT
  - surface/pièces : .surfacePiece     « 37 m² 3 pièces »
  - URL    : a.img_bien / .titreAnnonce a  → /immobilier/{slug}

Filtre département : le code postal est présent en clair sur la carte
(.localisation « VILLE (45600) ») → POST-FILTRE STRICT code_postal[:2] ∈
départements cibles, sans appel externe. On écarte aussi les locations (« €/mois »).
0 fuite.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import (
    get_with_retry,
    make_client,
    parse_int,
    parse_loc,
    parse_price,
    standalone_main,
)

BASE_URL = "https://www.agenceduchateau.fr"
LIST_URL = BASE_URL + "/immobilier/type/maison?page={page}"
MAX_PAGES = 8
PHOTOS_PER_CARD = 3


def _parse_card(card) -> dict | None:
    title_el = card.select_one(".titreAnnonce")
    a = title_el.select_one("a[href]") if title_el else card.select_one("a.img_bien")
    href = a.get("href") if a else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    price_el = card.select_one(".prixCoeur")
    price_txt = price_el.get_text(" ", strip=True) if price_el else ""
    if "/mois" in price_txt.lower():
        return None  # location, pas une vente
    prix = parse_price(price_txt)

    loc_el = card.select_one(".localisation")
    ville, code_postal = parse_loc(loc_el.get_text(" ", strip=True) if loc_el else "")

    sp_el = card.select_one(".surfacePiece")
    sp = sp_el.get_text(" ", strip=True) if sp_el else ""
    surface = None
    m = re.search(r"(\d[\d\s\xa0]*)\s*m²", sp)
    if m:
        try:
            surface = float(re.sub(r"[\s\xa0]", "", m.group(1)))
        except ValueError:
            surface = None
    pieces = parse_int(r"(\d+)\s*pi[èe]ce", sp)

    m_id = re.search(r"/immobilier/([^/?]+)", url)
    id_annonce = m_id.group(1) if m_id else url

    photos = []
    img = card.select_one("img")
    if img and img.get("src") and not img.get("src").startswith("data:"):
        src = img.get("src")
        photos.append(src if src.startswith("http") else BASE_URL + src)

    return {
        "source": "agence_du_chateau_45",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": "",
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Agence du Château (Sully-sur-Loire)",
    }


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()   # tous les biens vus (pour détecter la fin de pagination)
    kept_ids: set[str] = set()   # biens retenus (dédup résultats)

    async with make_client() as client:
        for page in range(1, MAX_PAGES + 1):
            r = await get_with_retry(client, LIST_URL.format(page=page))
            if r is None or r.status_code != 200:
                break
            cards = BeautifulSoup(r.text, "html.parser").select("div.bienAccueil")
            if not cards:
                break

            new_cards = 0   # cartes JAMAIS vues (avant filtrage) → fin de pagination
            kept = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue
                aid = bien.get("id_annonce")
                if aid not in seen_ids:
                    seen_ids.add(aid)
                    new_cards += 1
                cp = bien.get("code_postal") or ""
                if not cp or cp[:2] not in departements:
                    continue
                if aid in kept_ids:
                    continue
                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue
                kept_ids.add(aid)
                results.append(bien)
                kept += 1

            print(f"[AgenceChateau] Page {page}: {len(cards)} cartes ({new_cards} nouvelles), {kept} retenues (cumul {len(results)})")
            # On arrête quand la pagination ne ramène plus AUCUNE carte inédite
            # (et non quand 0 bien est retenu : un match peut être en page suivante).
            if new_cards == 0 and page > 1:
                break
            await asyncio.sleep(0.5)

    print(f"[AgenceChateau] Total {len(results)} annonces (départements cibles)")
    return results


if __name__ == "__main__":
    standalone_main(search, "Agence du Château (Sully-sur-Loire)")

"""scrapers/72immobilier.py — 72 Immobilier (agence locale Le Mans / Sarthe)

Méthode : scrape_simple (httpx) — SSR HTML (pas de JS nécessaire).

Agence locale du Mans : **100 % Sarthe (72)**, aucun bien hors-72.
Filtre département CÔTÉ SERVEUR via le listing global du département :
    /immobilier/Maisons-a-vendre-Sarthe-72            (page 1)
    /immobilier/Maisons-a-vendre-Sarthe-72/page-{N}   (pagination, 12/page)
→ ce listing ramène la totalité des maisons de la Sarthe de l'agence
  (toutes communes confondues), sans déborder sur les départements voisins.

Cartes : div.annonce_card
  - data-attrs du bouton .favorite-toggle : data-favorite-url / -price / -ref /
    -title / -city / -image  (prix & url fiables sans parsing texte)
  - .city          → "COULAINES - 72190"   → ville + code_postal
  - .price         → "892 500 €"           (secours)
  - .tr_type       → "achat - maison"
  - .annonce_desc  → titre / accroche
  - .details_bien .part_detail → 4 blocs icône+valeur :
      superficie.svg    → surface habitable (m²)
      espace-jardin.svg → surface terrain (m²)
      piece-maison.svg  → pièces
      chambre.svg       → chambres

Comme l'agence est uniquement en Sarthe, le scraper ne sollicite le site
que si le département 72 figure dans `criteres["departements"]` ; sinon il
renvoie [] immédiatement (0 requête). Post-filtre code_postal[:2] == "72"
par sécurité (0 fuite constatée).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.72immobilier.fr"
LISTING_PATH = "/immobilier/Maisons-a-vendre-Sarthe-72"
DEPT = "72"
MAX_PAGES = 15        # plafond ; ~5 pages suffisent aujourd'hui (12/page)
PER_PAGE = 12
PHOTOS_PER_CARD = 4


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]

    # Agence 100 % Sarthe : rien à faire si le 72 n'est pas ciblé.
    if departements and DEPT not in departements:
        print(f"[72Immo] Dept {DEPT} non ciblé — skip")
        return []

    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = BASE_URL + LISTING_PATH
            if page > 1:
                url += f"/page-{page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[72Immo] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(".annonce_card")
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

                # Sécurité 0-fuite : Sarthe uniquement.
                cp = bien.get("code_postal") or ""
                if cp[:2] != DEPT:
                    continue

                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                seen_ids.add(aid)
                results.append(bien)
                new_on_page += 1

            # Dernière page (listing partiel) ou plus rien de neuf → stop
            if len(cards) < PER_PAGE or new_on_page == 0:
                break

            await asyncio.sleep(0.5)

    print(f"[72Immo] Dept {DEPT}: {len(results)} annonces")
    return results


def _parse_card(card) -> dict | None:
    fav = card.select_one(".favorite-toggle")

    # URL / ref : priorité aux data-attrs du bouton favoris, puis à l'ancre.
    url = ""
    ref = ""
    if fav:
        url = fav.get("data-favorite-url", "") or ""
        ref = fav.get("data-favorite-ref", "") or ""
    if not url:
        a = card.select_one(".annonce_btn a[href], a[href*='/immobilier/maison/']")
        if a:
            href = a.get("href", "")
            url = href if href.startswith("http") else BASE_URL + href
    if not url:
        return None

    # id_annonce : id numérique du slug d'URL (…/maison-76772), puis ref.
    id_num = ""
    m_id = re.search(r"/maison-(\d+)", url)
    if m_id:
        id_num = m_id.group(1)
    id_annonce = id_num or ref or url

    # Localisation : "COULAINES - 72190"
    ville, cp = "", ""
    city_el = card.select_one(".city")
    if city_el:
        ville, cp = _parse_loc(city_el.get_text(" ", strip=True))
    if not cp:
        # secours : data-favorite-city (ville seule) + cp depuis l'URL
        m_cp = re.search(r"-(\d{5})/", url)
        if m_cp:
            cp = m_cp.group(1)
        if fav and not ville:
            ville = (fav.get("data-favorite-city", "") or "").title()

    # Prix : data-favorite-price (centimes ? non, euros entiers) puis .price texte.
    prix = None
    if fav and fav.get("data-favorite-price"):
        try:
            prix = float(re.sub(r"[^\d]", "", fav["data-favorite-price"]))
        except ValueError:
            prix = None
    if prix is None:
        price_el = card.select_one(".price")
        if price_el:
            prix = _parse_price(price_el.get_text(" ", strip=True))

    # Titre / accroche
    titre = ""
    if fav and fav.get("data-favorite-title"):
        titre = fav["data-favorite-title"].strip()
    if not titre:
        desc_el = card.select_one(".annonce_desc")
        if desc_el:
            titre = desc_el.get_text(" ", strip=True)
    if not titre:
        titre = f"Maison — {ville} ({cp})"

    # Détails (icône → valeur)
    surface = surface_terrain = pieces = chambres = None
    for pd in card.select(".details_bien .part_detail"):
        img = pd.select_one("img")
        strong = pd.select_one("strong")
        if not strong:
            continue
        icon = (img.get("src", "") if img else "").lower()
        val = strong.get_text(" ", strip=True)
        if "superficie" in icon:
            surface = _parse_num(val)
        elif "jardin" in icon or "terrain" in icon:
            surface_terrain = _parse_num(val)
        elif "piece" in icon:
            pieces = _parse_int(val)
        elif "chambre" in icon:
            chambres = _parse_int(val)

    # Photos
    photos = []
    if fav and fav.get("data-favorite-image"):
        photos.append(fav["data-favorite-image"])
    for img in card.select(".photo img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:") and src not in photos:
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "72immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": titre,
        "departement": cp[:2] if cp else DEPT,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "72 Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'COULAINES - 72190' → ('Coulaines', '72190')"""
    cp = ""
    m = re.search(r"(\d{5})", text)
    if m:
        cp = m.group(1)
    ville = re.sub(r"[-–]?\s*\d{5}\s*$", "", text).strip(" -–").title()
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text.split("€")[0])
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_num(text: str) -> float | None:
    """'4 450 m²' → 4450.0 ; '232 m²' → 232.0"""
    cleaned = re.sub(r"[\s\xa0]", "", text.split("m")[0]).replace(",", ".")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        f = float(cleaned) if cleaned else None
    except ValueError:
        return None
    return f if (f and f > 0) else None


def _parse_int(text: str) -> int | None:
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


# ── CLI standalone ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    biens = asyncio.run(
        search(
            {
                "departements": criteres.departements,
                "prix_max": criteres.prix_max,
                "prix_min": getattr(criteres, "prix_min", 0),
                "surface_min": criteres.surface_min,
            }
        )
    )
    print(f"\nTotal 72 Immobilier: {len(biens)} annonces")
    by_dept: dict[str, int] = {}
    for b in biens:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print("Par département:", dict(sorted(by_dept.items())))
    leaks = [b for b in biens if b["code_postal"][:2] != "72"]
    print(f"FUITES hors-72: {len(leaks)}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:48]}"
            f" — {b['prix']}€ — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b.get('pieces') or '?'}p — {b['ville']}"
        )

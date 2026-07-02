"""scrapers/latelier_immo.py — L'Atelier de l'Immo (agence locale Yonne)

Méthode : scrape_simple (httpx) — SSR HTML (rendu serveur, pas de JS requis).
Site   : https://www.latelier-immo.com — agence locale Auxerre / Sens / Yonne (89).

URL pattern : /vente/{page}  (ex: /vente/1, /vente/2, /vente/3) — pagination
              globale. Pas de filtre département dans l'URL : c'est une agence
              MONO-DÉPARTEMENT (Yonne, CP 89xxx). On scrape l'inventaire complet
              (~3 pages, ~22 biens) puis POST-FILTRE strict code_postal[:2]==dept.
              (slugs ville /vente/{id}-{ville}/{type}/{page} existent mais inutiles
               pour un filtre département — un seul dept couvert.)

Cartes : div.card_bien
  - URL   : a.card_bien__link[href] → /vente/{id}-{ville}/{type}/t{N}/{num-slug}/
  - Titre : h2.card_bien__title → "Maison 8 pièce(s) 4 chambre(s) 209.76 m²"
            (porte type + pièces + chambres + surface habitable)
  - Loc   : p.card_bien__localisation → "Beine (89800)"
  - Prix  : p.card_bien__prix → "328 000 €"
  - Photos: img[src] sous .card_bien__swiper (//jtroisg.staticlbi.com/...)
  - Type  : segment d'URL (maison, propriete, appartement, studio, garage…)

Type de bien : on ne garde que maisons / propriétés (exclut appartement, studio,
               garage, terrain, local…).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.latelier-immo.com"
MAX_PAGES = 8
PHOTOS_PER_CARD = 10


# Types de bien (segment d'URL) à conserver : maisons / propriétés.
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Agence mono-département (Yonne) : si 89 n'est pas demandé, rien à faire.
    if "89" not in departements:
        print("[AtelierImmo] Dept 89 non demandé — agence Yonne, 0 annonce")
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/vente/{page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[AtelierImmo] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.card_bien")
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

                # Post-filtre département STRICT (0 fuite hors-zone).
                cp = bien["code_postal"]
                if not cp or cp[:2] not in departements:
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
                bien["departement"] = cp[:2]
                results.append(bien)
                new_on_page += 1

            if new_on_page == 0 and len(cards) == 0:
                break
            await asyncio.sleep(0.5)

    print(f"[AtelierImmo] Total: {len(results)} annonces (dept 89)")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a.card_bien__link")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL : /vente/{id}-{ville}/{type}/t{N}/{slug}/
    parts = [p for p in href.split("/") if p]
    type_seg = parts[2] if len(parts) > 2 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean) and not _KEEP_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        return None
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # id_annonce : numéro en tête du dernier segment slug
    id_annonce = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_annonce = m.group(1)
    if not id_annonce:
        id_annonce = url

    # Titre : "Maison 8 pièce(s) 4 chambre(s) 209.76 m²" (porte pièces/chambres/surface)
    title_el = card.select_one("h2.card_bien__title")
    titre_raw = title_el.get_text(" ", strip=True) if title_el else ""
    titre_raw = re.sub(r"\s+", " ", titre_raw).strip()

    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", titre_raw)
    chambres = _parse_int(r"(\d+)\s*chambre", titre_raw)
    surface = _parse_surface(titre_raw)

    # Pièces en secours : segment t{N} de l'URL
    if pieces is None:
        for seg in parts:
            m = re.match(r"^t(\d+)$", seg)
            if m:
                pieces = int(m.group(1))
                break

    # Localisation : "Beine (89800)"
    loc_el = card.select_one("p.card_bien__localisation")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)

    # Prix
    price_el = card.select_one("p.card_bien__prix")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Titre lisible (segment slug détaillé en priorité, sinon synthèse)
    titre = type_bien.title()
    if len(parts) >= 1:
        slug = re.sub(r"^\d+-", "", parts[-1]).replace("-", " ").strip()
        if slug:
            titre = slug
    titre = titre[:150] or f"{type_bien.title()} {ville}".strip()

    # Photos
    photos = []
    for img in card.select(".card_bien__swiper img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "latelier_immo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre,
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
        "photos": photos,
        "dpe": None,
        "agence": "L'Atelier de l'Immo",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Beine (89800)' → ('Beine', '89800')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_surface(text: str) -> float | None:
    """'... 209.76 m²' → 209.76"""
    m = re.search(r"([\d]+(?:[.,]\d+)?)\s*m²", text)
    if m:
        try:
            f = float(m.group(1).replace(",", "."))
            if 8 <= f <= 5000:
                return f
        except ValueError:
            pass
    return None


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
    print(f"\nTotal L'Atelier de l'Immo: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p/{b.get('chambres') or '?'}ch"
            f" — {b['type_bien']} — {b['ville']}"
        )

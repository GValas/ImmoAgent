"""scrapers/occitanie_immobilier.py — Occitanie Immobilier (agence locale Gruissan / Aude)

Méthode : scrape_simple (httpx) — SSR HTML (CMS LBI, même famille que Le Tuc).
URL pattern : /nos-biens          (page 1)
              /nos-biens/{N}      (pages suivantes, N >= 2)
              → liste NATIONALE de l'agence, SANS filtre département serveur.
              L'agence ne couvre QUE l'Aude (11, littoral Gruissan/Narbonne) →
              on post-filtre strictement sur code_postal[:2] (0 fuite garantie).

Cartes : <article class="card_bien__structure">
  - URL   : premier a[href] → /vente/{id-cityslug}/{type}/{id-slug}
            (attention : le 1er segment est un ID interne de ville, PAS un dept)
  - Loc   : .card_bien__localisation  →  "Ville (CODEPOSTAL)"
  - Prix  : .card_bien__prix          →  "265 000 €"
  - Titre : .card_bien__title         →  "Maison ... 5 pièce(s) 4 chambre(s) 198 m²"
            (type de bien = 1er mot ; pièces/chambres/surface dans le texte)
  - Photos: picture img[src] / source[srcset]  (host //occitanie-immobilier.staticlbi.com)

Type de bien : 1er mot du titre / segment d'URL (maison, villa, appartement,
               chalet, studio, duplex, terrain, garage, immeuble...).
               On ne garde que maisons / villas / propriétés.

Couverture : agence mono-département (Aude 11). HORS zone cible actuelle
             (72/28/45/89…) → le scraper est fonctionnel mais renvoie 0 bien
             sur les départements cibles. Conservé pour réactivation si la zone
             cible inclut un jour l'Aude.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.occitanie-immobilier.fr"
LIST_PATH = "/nos-biens"
MAX_PAGES = 12
PHOTOS_PER_CARD = 10


# Types de bien à conserver (maisons / propriétés)
_KEEP_TYPE = re.compile(
    r"maison|villa|propriete|propriété|chalet|mas|domaine|demeure|longere|longère|"
    r"ferme|manoir|chateau|château|moulin|gite|gîte|bastide",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|duplex|terrain|local|commerce|garage|parking|immeuble|"
    r"bureau|fonds|rez-de-villa",
    re.IGNORECASE,
)


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
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            path = LIST_PATH if page == 1 else f"{LIST_PATH}/{page}"
            url = BASE_URL + path
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[OccitanieImmo] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                "article.card_bien__structure"
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

                # Post-filtre département STRICT (0 fuite hors-zone)
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

                bien["departement"] = cp[:2]
                seen_ids.add(aid)
                results.append(bien)
                new_on_page += 1

            print(
                f"[OccitanieImmo] Page {page}: {len(cards)} cartes, "
                f"{new_on_page} retenues (zone)"
            )
            await asyncio.sleep(0.6)

    print(f"[OccitanieImmo] Total zone cible: {len(results)} annonces")
    return results


def _parse_card(card) -> dict | None:
    link = card.find("a", href=True)
    href = link["href"] if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    parts = [p for p in href.split("/") if p]
    # /vente/{id-cityslug}/{type}/{id-slug}
    type_seg = parts[2] if len(parts) > 2 else ""

    # Localisation : "Ville (CODEPOSTAL)"
    loc_el = card.select_one(".card_bien__localisation")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)

    # Titre (contient type + pièces/chambres/surface)
    title_el = card.select_one(".card_bien__title")
    titre_raw = title_el.get_text(" ", strip=True) if title_el else ""
    titre = re.sub(r"\s+", " ", titre_raw).strip()

    # Type de bien : 1er mot du titre, repli sur le segment d'URL
    type_bien = ""
    m_t = re.match(r"^([A-Za-zÀ-ÿ\-]+)", titre)
    if m_t:
        type_bien = m_t.group(1).lower()
    if not type_bien and type_seg:
        type_bien = type_seg.lower()

    # Filtre type : maisons / propriétés uniquement
    type_ref = f"{type_bien} {type_seg}"
    if _EXCLUDE_TYPE.search(type_ref):
        return None
    if not _KEEP_TYPE.search(type_ref):
        return None

    # id_annonce : id numérique du dernier segment d'URL
    id_annonce = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_annonce = m.group(1)
    if not id_annonce:
        m2 = re.search(r"idbien=(\d+)", str(card))
        id_annonce = m2.group(1) if m2 else url

    # Prix
    price_el = card.select_one(".card_bien__prix")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Pièces / chambres / surface depuis le texte du titre
    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", titre)
    chambres = _parse_int(r"(\d+)\s*chambre", titre)
    surface = _parse_surface(titre)

    # Pièces en secours : segment tN éventuel
    if pieces is None:
        m_t = re.search(r"\bt(\d+)\b", href, re.IGNORECASE)
        if m_t:
            pieces = int(m_t.group(1))

    # Photos
    photos = []
    for pic in card.select("picture"):
        src = ""
        img = pic.find("img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
        if not src:
            source = pic.find("source")
            if source:
                src = (source.get("srcset") or "").split()[0]
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    # dédup en gardant l'ordre
    seen = set()
    photos = [p for p in photos if not (p in seen or seen.add(p))][:PHOTOS_PER_CARD]

    titre_clean = titre or f"{type_bien.title()} {ville}".strip()

    return {
        "source": "occitanie_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre_clean[:150],
        "type_bien": type_bien or "maison",
        "description": "",
        "departement": code_postal[:2] if code_postal else None,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Occitanie Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Gruissan (11430)' → ('Gruissan', '11430')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_surface(text: str) -> float | None:
    """'... 5 pièce(s) 4 chambre(s) 198 m²' → 198.0"""
    if not text:
        return None
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
    print(f"\nTotal Occitanie Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

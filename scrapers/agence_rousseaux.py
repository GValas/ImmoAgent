"""scrapers/agence_rousseaux.py — Agence Rousseaux Immobilier (Sablé-sur-Sarthe, 72)

Méthode : scrape_simple (httpx) — SSR HTML (CMS La Boîte Immo / Interkab, thème « Levant »).
URL pattern : /vente/{page}   (ex: /vente/1, /vente/2 …)
              → liste TOUTES les villes de l'agence (bocage sabolien : 72 + débord 53/49),
                PAS de filtre département côté serveur → post-filtre strict CP[:2].

Cartes : article.item
  - URL    : a[href]  → /vente/1-{ville}/{type}/{id}-{slug}   (« Voir le bien »)
  - id     : button.js-selectionToggle[data-add-url="/i/selection/addbien?idbien=NNNN"]
             (repli : id numérique dans le segment final de l'URL)
  - Loc    : .item__block--city  →  "Sablé-sur-Sarthe (72300)"
  - Prix   : .item__price        →  "86 100 €"
  - Type / pièces / chambres / surface : .item__block--title
             (« Maison 9 pièce(s) 4 chambre(s) 235 m² » — surface = dernier m²).
             Le type est aussi présent dans le segment d'URL → on le fiabilise depuis l'URL.
  - Options: .item__options  →  terrain, balcon, ascenseur… (libellés via aria-label/title)
  - Photos : article picture img[src]  (CDN //…staticlbi.com/…)

Type de bien : déduit du segment d'URL (/maison/, /appartement/, /immeuble/, /terrain/…).
               On ne conserve que maisons / propriétés / villas / fermes…

Couverture : agence mono-secteur (Sablé-sur-Sarthe). Petit stock mais réel,
             essentiellement 72, avec quelques biens 53 / 49 limitrophes (tous cibles).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int

BASE_URL = "https://www.agence-rousseaux.fr"
MAX_PAGES = 8
PHOTOS_PER_CARD = 10


# Types de bien (segment d'URL) à conserver : maisons / propriétés / fermes…
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|maison-de-village|"
    r"maison-de-ville|maison-bourgeoise",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"viager|cave|box",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

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
                print(f"[Rousseaux] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("article.item")
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

                # Post-filtre département STRICT (pas de filtre serveur) → 0 fuite.
                dept = bien["code_postal"][:2] if bien["code_postal"] else ""
                if dept not in departements:
                    continue
                bien["departement"] = dept

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

            # Plus aucune nouvelle annonce (pagination qui boucle/épuisée) → stop
            if new_on_page == 0:
                break

            await asyncio.sleep(0.5)

    print(f"[Rousseaux] Total : {len(results)} annonces (zone cible)")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one(".item__links a[href]") or card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL : /vente/1-{ville}/{type}/{id}-{slug}
    parts = [p for p in href.split("/") if p]
    type_seg = parts[2] if len(parts) > 2 else ""
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        # type inconnu/ambigu → exclusion par prudence
        return None
    type_bien = type_seg.replace("-", " ").strip() or "maison"

    # id_annonce : data-add-url="/i/selection/addbien?idbien=NNNN"
    id_annonce = ""
    btn = card.select_one("[data-add-url]")
    if btn:
        m = re.search(r"idbien=(\d+)", btn.get("data-add-url", ""))
        if m:
            id_annonce = m.group(1)
    if not id_annonce and len(parts) > 3:
        m = re.match(r"^(\d+)-", parts[3])
        if m:
            id_annonce = m.group(1)
    id_annonce = id_annonce or url

    # Localisation : "Sablé-sur-Sarthe (72300)"
    city_el = card.select_one(".item__block--city")
    loc = city_el.get_text(" ", strip=True) if city_el else ""
    ville, code_postal = _parse_loc(loc)
    if not code_postal:
        return None

    # Prix
    price_el = card.select_one(".item__price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Caractéristiques : « Maison 9 pièce(s) 4 chambre(s) 235 m² » (surface = dernier m²)
    feat_el = card.select_one(".item__block--title") or card.select_one(".item__title")
    feat_text = re.sub(r"\s+", " ", feat_el.get_text(" ", strip=True)) if feat_el else ""
    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", feat_text)
    chambres = _parse_int(r"(\d+)\s*chambre", feat_text)
    surface = _parse_surface(feat_text)

    # Terrain non fiable dans la vue liste (libellés absents, chiffres éclatés sur
    # plusieurs cellules) → laissé à None (enrichi en page détail par gallery si besoin).
    surface_terrain = None

    # Titre : slug d'URL (lisible), repli sur la ligne caractéristiques.
    titre = url.rstrip("/").split("/")[-1]
    titre = re.sub(r"^\d+-", "", titre).replace("-", " ").strip().capitalize()
    if not titre:
        titre = feat_text or f"{type_bien.title()} {ville}".strip()

    # Photos (CDN //…staticlbi.com)
    photos = []
    for img in card.select("picture img, img.media-js"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "agence_rousseaux",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Agence Rousseaux Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Sablé-sur-Sarthe (72300)' → ('Sablé-sur-Sarthe', '72300')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """Surface habitable = dernière mention « NNN m² » de la ligne caractéristiques."""
    matches = re.findall(r"([\d]+(?:[.,]\d+)?)\s*m²", text)
    if matches:
        try:
            f = float(matches[-1].replace(",", "."))
            if 5 <= f <= 5000:
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
    print(f"\nTotal Agence Rousseaux: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

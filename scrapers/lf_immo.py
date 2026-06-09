"""scrapers/lf_immo.py — LF Immo (réseau de mandataires, plateforme LaFourmi-Immo)

Méthode : scrape_simple (httpx) — SSR HTML.
Site : https://www.lfimmo.fr

⚠️ Pas de filtre département serveur exploitable en httpx.
Le formulaire de recherche utilise un champ `q[near_search][city]` géocodé
côté client par Mapbox (autocomplete) + un rayon : le backend attend des
coordonnées résolues par session, pas une simple chaîne ville/CP. Tous les
essais de `near_search[city]=<ville>` / `<CP>` en httpx pur renvoient 0 carte.
→ On scrape donc la **liste nationale** `/achat` paginée (6 cartes/page,
~250 pages) puis on applique un **post-filtre STRICT `code_postal[:2]`** sur les
départements cibles. Objectif : 0 fuite hors-zone.

URL pattern :
  /achat?page=N&q[with_folder_category]=sale&q[with_condition]=available
  (option type maison : q[with_types][]=house / propriete)

Cartes : article.card-property
  - URL    : a.card-link[href]  → /annonces/{id}-vente-{type}-{N}pieces-{ville}-{cp}
  - Prix   : .card-title strong.text-theme  → "470 000 €"
  - Titre  : h3.card-subtitle a
  - Loc    : p.card-text  →  "Ville, CODEPOSTAL"
  - Détails: ul.list-inline li → surface hab (fi-living-area), terrain
             (fi-land-area), pièces (fi-rooms-count), chambres (fi-bedroom-count)
  - Photos : img.object-fit-cover[src]
  - Réf    : id_annonce = id numérique du slug d'URL

Type de bien : déduit du segment d'URL après "vente-".

Couverture : réseau national à forte implantation Alsace / Sud-Ouest /
Île-de-France ; implantation faible voire nulle sur le Val-de-Loire.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.lfimmo.fr"
MAX_PAGES = 260          # ~250 pages de catalogue national
PHOTOS_PER_CARD = 1      # une seule miniature dans la carte liste

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien (segment d'URL) à conserver : maisons / propriétés...
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|chalet",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|hangar|cave|box",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
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
        for page in range(1, MAX_PAGES + 1):
            params = {
                "page": str(page),
                "q[with_folder_category]": "sale",
                "q[with_condition]": "available",
            }
            try:
                r = await client.get(f"{BASE_URL}/achat", params=params)
            except Exception as e:
                print(f"[LFImmo] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select("article.card-property")
            if not cards:
                break

            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # Post-filtre STRICT département (national → cible) : 0 fuite
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

            # Fin de pagination (plus de lien "next")
            if not soup.find("link", rel="next"):
                break

            await asyncio.sleep(0.5)

    print(f"[LFImmo] Total: {len(results)} annonces (post-filtre dept)")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a.card-link")
    href = link.get("href", "") if link else ""
    if not href or "/annonces/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce : id numérique en tête de slug
    m_id = re.search(r"/annonces/(\d+)-", href)
    id_annonce = m_id.group(1) if m_id else url

    # Type de bien depuis le slug : /annonces/{id}-vente-{type}-...
    m_type = re.search(r"/annonces/\d+-vente-([a-zàâ]+)", href, re.IGNORECASE)
    type_seg = m_type.group(1) if m_type else ""
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg.lower()

    # Localisation : "Ville, CODEPOSTAL"
    loc_el = card.select_one("p.card-text")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)
    if not code_postal:
        # secours : CP en fin de slug
        m_cp = re.search(r"-(\d{5})$", href)
        if m_cp:
            code_postal = m_cp.group(1)

    # Titre
    title_el = card.select_one("h3.card-subtitle a")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Prix
    price_el = card.select_one(".card-title strong.text-theme")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Détails (surface, terrain, pièces, chambres) via icônes
    surface = surface_terrain = None
    pieces = chambres = None
    for li in card.select("ul.list-inline li"):
        icon = li.find("i")
        cls = " ".join(icon.get("class", [])) if icon else ""
        val_el = li.find("span")
        val = val_el.get_text(" ", strip=True) if val_el else ""
        if "living-area" in cls:
            surface = _parse_num(val)
        elif "land-area" in cls:
            surface_terrain = _parse_num(val)
        elif "rooms-count" in cls:
            pieces = _parse_int(val)
        elif "bedroom-count" in cls:
            chambres = _parse_int(val)

    # Photo (miniature de la carte)
    photos = []
    img = card.select_one("img.object-fit-cover")
    src = img.get("src") if img else None
    if src and not src.startswith("data:"):
        if src.startswith("//"):
            src = "https:" + src
        photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "lf_immo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "LF Immo",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Thann, 68800' → ('Thann', '68800')
    'Bouglainval, 28130 - 15,4 km' → ('Bouglainval', '28130')
    (le suffixe distance ' - N km' apparaît sur la liste nationale)."""
    # Retire un éventuel suffixe distance "- 15,4 km"
    text = re.sub(r"\s*-\s*[\d.,]+\s*km\s*$", "", text, flags=re.IGNORECASE)
    cp = ""
    m_cp = re.search(r"(\d{5})", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r",?\s*\d{5}.*$", "", text).strip().strip(",").strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text.split("/")[0])
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_num(text: str) -> float | None:
    """'335 m²' → 335.0"""
    m = re.search(r"([\d\s\xa0]+)", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
        return f if f > 0 else None
    except ValueError:
        return None


def _parse_int(text: str) -> int | None:
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


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
    print(f"\nTotal LF Immo: {len(biens)} annonces")
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

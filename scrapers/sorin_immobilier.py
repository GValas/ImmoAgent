"""scrapers/sorin_immobilier.py — Sorin Immobilier (Château-Gontier, Sud-Mayenne, 53)

Méthode : scrape_simple (httpx) — SSR HTML (CMS "Côté Immobilier" récent, thème 2).
URL pattern : /annonces/transaction/vente.html?page=N
              → liste de l'agence (Château-Gontier + sud Mayenne, déborde sur 35/49/44).
                PAS de code postal dans la carte de liste (seul le nom de ville y figure,
                parfois ambigu, ex. « Fougères » est à cheval 35/53). On récupère donc
                le CP exact sur la PAGE DÉTAIL (span.postal-code) avant d'appliquer le
                POST-FILTRE strict CP[:2] → 0 fuite garanti.

Cartes liste : div.listing-item
  - URL    : a.product-image[href*="fiches/"]
  - Titre  : .product-name
  - Prix   : .product-price            → "138 600 €"
  - Surface: span.data-list__item--Surface .data-list__item--value
  - Pièces : span.data-list__item--NbPiece .data-list__item--value
  - Réf    : span.data-list__item--products_model .data-list__item--value
  - Photo  : img.photo[src]
Page détail :
  - CP     : span.postal-code          → "53500"
  - Ville  : bloc « Ville » du tableau caractéristiques

Type de bien : déduit du titre (maison / fermette / propriété... ; exclut appart/terrain).

Couverture : agence Château-Gontier (53) — stock réel modeste. dernier_test 2026-06-09.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.sorin-immobilier.com"
LIST_URL = BASE_URL + "/annonces/transaction/vente.html"
MAX_PAGES = 6
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|pavillon|grange|fermette|bastide",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"boutique|hangar|studio",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1) collecte des cartes de liste (sans CP) qui passent les bornes prix/surface
        prelim: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            try:
                r = await client.get(f"{LIST_URL}?page={page}")
            except Exception as e:
                print(f"[Sorin] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.listing-item")
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
                if bien["id_annonce"] in seen_ids:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                seen_ids.add(bien["id_annonce"])
                prelim.append(bien)
                new_on_page += 1

            if new_on_page == 0 and page > 1:
                break
            await asyncio.sleep(0.5)

        # 2) résolution du CP exact sur la fiche détail + POST-FILTRE dept STRICT
        for bien in prelim:
            cp, ville = await _fetch_cp(client, bien["url"])
            if not cp or cp[:2] not in departements:
                continue
            bien["code_postal"] = cp
            bien["departement"] = cp[:2]
            if ville:
                bien["ville"] = ville[:80]
            results.append(bien)
            await asyncio.sleep(0.4)

    print(f"[Sorin] Total: {len(results)} annonces")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one('a.product-image[href*="fiches/"]') or card.select_one(
        'a[href*="fiches/"]'
    )
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = _abs(href)

    name_el = card.select_one(".product-name")
    titre = name_el.get_text(" ", strip=True) if name_el else ""
    # « Fermette - Saint Pierre Des Landes , Fougeres » → garde la 1ʳᵉ ville
    titre = re.sub(r"\s+,\s+", " - ", titre).strip(" -")

    # Le type est dans le 1ᵉʳ segment du titre (avant la ville) ; détecter sur
    # ce seul segment évite de prendre « chateau » dans la ville « Château-Gontier ».
    type_seg = titre.split(" - ", 1)[0]
    type_bien = _detect_type(type_seg)
    if type_bien is None:
        return None

    price_el = card.select_one(".product-price")
    prix = _parse_price(price_el.get_text(" ", strip=True)) if price_el else None

    surface_raw = _data_value(card, "Surface")
    surface = float(surface_raw.replace(",", ".")) if surface_raw else None
    pieces_raw = _data_value(card, "NbPiece")
    pieces = int(float(pieces_raw)) if pieces_raw else None

    ref = _data_value(card, "products_model") or _id_from_url(href)
    id_annonce = ref or url

    photos = []
    img = card.select_one("img.photo")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(_abs(src))

    return {
        "source": "sorin_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": "",
        "ville": "",
        "code_postal": "",
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Sorin Immobilier",
    }


async def _fetch_cp(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """Récupère (code_postal, ville) sur la page détail."""
    try:
        r = await client.get(url)
    except Exception:
        return "", ""
    if r.status_code != 200:
        return "", ""
    s = BeautifulSoup(r.text, "html.parser")
    cp = ""
    cp_el = s.select_one("span.postal-code")
    if cp_el:
        m = re.search(r"(\d{5})", cp_el.get_text())
        if m:
            cp = m.group(1)
    if not cp:
        m = re.search(r'postal-code[^0-9]{0,20}(\d{5})', r.text)
        if m:
            cp = m.group(1)
    # Ville : bloc « Ville » du tableau caractéristiques
    ville = ""
    m_v = re.search(r"Ville\s*</div>\s*<div[^>]*>\s*<b>\s*([^<]+?)\s*</b>", r.text)
    if m_v:
        ville = m_v.group(1).strip().title()
    return cp, ville


# ── Helpers ──────────────────────────────────────────────────────────────────

def _abs(href: str) -> str:
    if href.startswith("http"):
        return href
    return BASE_URL + "/" + href.lstrip("./")


def _detect_type(text: str) -> str | None:
    # « Château-Gontier » est une ville, pas un type de bien : on la neutralise.
    cleaned = re.sub(r"ch[aâ]teau[\s-]*gontier", " ", text, flags=re.IGNORECASE)
    if _EXCLUDE_TYPE.search(cleaned) and not _KEEP_TYPE.search(cleaned):
        return None
    m = _KEEP_TYPE.search(cleaned)
    if m:
        return m.group(0).lower()
    # titre sans type explicite (commence par la ville) → maison par défaut,
    # sauf si un type exclu apparaît.
    if _EXCLUDE_TYPE.search(cleaned):
        return None
    return "maison"


def _data_value(card, key: str) -> str:
    el = card.select_one(
        f"span.data-list__item--{key} .data-list__item--value"
    )
    return el.get_text(strip=True) if el else ""


def _parse_price(text: str) -> float | None:
    m = re.search(r"([\d\s\xa0]{4,})\s*€", text)
    if not m:
        return None
    cleaned = re.sub(r"[^\d]", "", m.group(1))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _id_from_url(href: str) -> str:
    m = re.search(r"_(\d+)/", href)
    return m.group(1) if m else ""


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
    print(f"\nTotal Sorin Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

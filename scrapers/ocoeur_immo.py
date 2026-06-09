"""scrapers/ocoeur_immo.py — Ô cœur de l'immo (Cholet / Mauges, 49)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress, thème "ocdl").
URL pattern : /annonces/vente/?paged-1=N
              → liste de l'agence (Cholet / Sèvremoine / Beaupréau) sans filtre
                dept côté serveur ; quelques biens en bordure (44 Nantes, 85
                Mortagne-sur-Sèvre) apparaissent → POST-FILTRE strict CP[:2].

Cartes : article.annonces
  - URL     : h2 a[href*="/annonce/"]
  - Titre   : h2 a
  - CP      : .desc-ville .code-postal   →  "49300"
  - Ville   : .desc-ville .ville
  - Prix    : .prix-annonce              →  "183 000"
  - Pièces  : .nb-room-annonce           →  "5  pièce(s)"
  - Chambres: .nb-bedroom-annonce        →  "3 chambre(s)"
  - Surface : .nb-surface                →  "96.20 m²"
  - Type    : classe CSS de l'article (category-annonces-vente-maison...)
  - Photo   : picture img[data-lazy-src]

Couverture : agence Cholet (49) — bon stock régional. dernier_test 2026-06-09.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://ocoeurdelimmo.immo"
LIST_URL = BASE_URL + "/annonces/vente/"
MAX_PAGES = 8
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
        for page in range(1, MAX_PAGES + 1):
            url = LIST_URL if page == 1 else f"{LIST_URL}?paged-1={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[OCoeur] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("article.annonces")
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

                # POST-FILTRE dept STRICT (0 fuite)
                if not bien["code_postal"] or bien["code_postal"][:2] not in departements:
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

            if new_on_page == 0 and page > 1:
                break
            await asyncio.sleep(0.5)

    print(f"[OCoeur] Total: {len(results)} annonces")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one('h2 a[href*="/annonce/"]') or card.select_one(
        'a[href*="/annonce/"]'
    )
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    titre = link.get_text(" ", strip=True) if link else ""

    cp_el = card.select_one(".desc-ville .code-postal")
    code_postal = ""
    if cp_el:
        m = re.search(r"(\d{5})", cp_el.get_text())
        if m:
            code_postal = m.group(1)
    ville_el = card.select_one(".desc-ville .ville")
    ville = ville_el.get_text(" ", strip=True) if ville_el else ""

    # Type : classes CSS de l'article + titre
    classes = " ".join(card.get("class", []))
    type_bien = _detect_type(f"{classes} {titre}")
    if type_bien is None:
        return None
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    price_el = card.select_one(".prix-annonce")
    prix = _parse_price(price_el.get_text(" ", strip=True)) if price_el else None

    pieces = _parse_int(r"(\d+)", _txt(card, ".nb-room-annonce"))
    chambres = _parse_int(r"(\d+)", _txt(card, ".nb-bedroom-annonce"))
    surface = _parse_surface(_txt(card, ".nb-surface"))

    id_annonce = _id_from_classes(card) or url

    photos = []
    img = card.select_one("picture img")
    if img:
        src = img.get("data-lazy-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            photos.append(src)

    return {
        "source": "ocoeur_immo",
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
        "agence": "Ô cœur de l'immo",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _txt(card, sel: str) -> str:
    el = card.select_one(sel)
    return el.get_text(" ", strip=True) if el else ""


def _detect_type(text: str) -> str | None:
    # le titre commence souvent par "Maison à vendre ..." / "Appartement ..."
    if _EXCLUDE_TYPE.search(text) and not _KEEP_TYPE.search(text):
        return None
    m = _KEEP_TYPE.search(text)
    if m:
        return m.group(0).lower()
    return None


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def _parse_surface(text: str) -> float | None:
    m = re.search(r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
        try:
            f = float(val)
            if 8 <= f <= 2000:
                return f
        except ValueError:
            pass
    return None


def _id_from_classes(card) -> str:
    for cls in card.get("class", []):
        m = re.match(r"post-(\d+)", cls)
        if m:
            return m.group(1)
    return ""


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
    print(f"\nTotal Ô cœur de l'immo: {len(biens)} annonces")
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

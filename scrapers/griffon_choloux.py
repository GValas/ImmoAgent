"""scrapers/griffon_choloux.py — Griffon Choloux Immobilier (Cholet / Beaupréau, 49)

Méthode : scrape_simple (httpx) — SSR HTML (CMS "Côté Immobilier" / Periimmo).
URL pattern : /annonces/transaction/vente.html?page=N
              → liste NATIONALE de l'agence (pas de filtre dept côté serveur).
                L'agence est implantée dans le Maine-et-Loire (Cholet / Beaupréau)
                donc le stock est quasi-exclusivement 49, mais quelques biens en
                bordure (44, 85) apparaissent → POST-FILTRE strict CP[:2] obligatoire.

Cartes : div.item-product-listing
  - URL    : a[href*="fiches/"]  → ../fiches/{T}-{N}-{N}_{id}/slug.html
  - Titre  : .products-name
  - Prix   : .products-price     → "151 390 €" (+ honoraires en sous-span ignoré)
  - Loc    : .products-localisation → "49450 SAINT ANDRE DE LA MARCHE"  (CP + ville !)
  - Texte  : .products-description (multi-ligne, surface/terrain parfois mentionnés)
  - Photo  : img.photo-listing[src]

Type de bien : déduit du titre / de la description (on ne garde que maisons /
               propriétés / pavillons / longères ; exclut appartement / terrain...).

Couverture : agence mono-secteur Cholet (49) — stock réel mais modeste.
             dernier_test 2026-06-09.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.griffon-choloux-immobilier.com"
LIST_URL = BASE_URL + "/annonces/transaction/vente.html"
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
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps de ferme|pavillon|grange|"
    r"fermette|bastide",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"boutique|hangar|entrepot|entrepôt|studio",
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
            url = f"{LIST_URL}?page={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Griffon] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                "div.item-product-listing"
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
                bien["departement"] = bien["code_postal"][:2]
                results.append(bien)
                new_on_page += 1

            if new_on_page == 0 and page > 1:
                break
            await asyncio.sleep(0.5)

    print(f"[Griffon] Total: {len(results)} annonces")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one('a[href*="fiches/"]')
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = _abs(href)

    name_el = card.select_one(".products-name")
    titre = name_el.get_text(" ", strip=True) if name_el else ""

    loc_el = card.select_one(".products-localisation")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    code_postal, ville = _parse_loc(loc)

    desc_el = card.select_one(".products-description")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Type : titre + description
    type_bien = _detect_type(f"{titre} {description}")
    if type_bien is None:
        return None
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    price_el = card.select_one(".products-price")
    prix = _parse_price(price_el) if price_el else None

    surface = _parse_surface(f"{titre} {description}")
    surface_terrain = _parse_terrain(description)
    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", description)
    chambres = _parse_int(r"(\d+)\s*chambre", description)

    id_annonce = _id_from_url(href) or url

    photos = []
    img = card.select_one("img.photo-listing")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(_abs(src))

    return {
        "source": "griffon_choloux",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Griffon Choloux Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _abs(href: str) -> str:
    if href.startswith("http"):
        return href
    return BASE_URL + "/" + href.lstrip("./")


def _detect_type(text: str) -> str | None:
    if _EXCLUDE_TYPE.search(text) and not _KEEP_TYPE.search(text):
        return None
    m = _KEEP_TYPE.search(text)
    if m:
        return m.group(0).lower()
    return None


def _parse_loc(text: str) -> tuple[str, str]:
    """'49450 SAINT ANDRE DE LA MARCHE' → ('49450', 'Saint Andre De La Marche')"""
    m = re.match(r"\s*(\d{5})\s+(.*)", text)
    if m:
        return m.group(1), m.group(2).strip().title()
    cp = ""
    m_cp = re.search(r"\b(\d{5})\b", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\b\d{5}\b", "", text).strip().title()
    return cp, ville


def _parse_price(el) -> float | None:
    # Le prix peut contenir un sous-span honoraires ; on garde le 1er nombre.
    txt = el.get_text(" ", strip=True)
    m = re.search(r"([\d\s\xa0]{4,})\s*€", txt)
    if not m:
        return None
    cleaned = re.sub(r"[^\d]", "", m.group(1))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_surface(text: str) -> float | None:
    m = re.search(
        r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m²?\s*(?:hab|habitable|de surface)",
        text, re.IGNORECASE,
    )
    if not m:
        # 'NNN m²' générique mais PAS précédé de 'terrain' (sinon on lit la
        # surface du terrain comme surface habitable).
        for cand in re.finditer(r"(\d{2,4}(?:[.,]\d+)?)\s*m²", text):
            prefix = text[max(0, cand.start() - 12):cand.start()].lower()
            if "terrain" in prefix:
                continue
            m = cand
            break
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
        try:
            f = float(val)
            if 8 <= f <= 2000:
                return f
        except ValueError:
            pass
    return None


def _parse_terrain(text: str) -> float | None:
    m = re.search(r"terrain[^0-9]{0,12}([\d\s\xa0]{2,})\s*m", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 50 <= f <= 5_000_000:
                return f
        except ValueError:
            pass
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
    print(f"\nTotal Griffon Choloux: {len(biens)} annonces")
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

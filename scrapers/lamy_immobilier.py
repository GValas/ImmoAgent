"""scrapers/lamy_immobilier.py — Lamy Immobilier (réseau national, ~250 agences)

Méthode : scrape_simple (httpx) — SSR HTML (cards rendues côté serveur).
URL pattern : /acheter/acheter-un-bien/annonces-de-biens-a-vendre/{region}/{dept-slug}[?page=N]
              ex: .../centre-val-de-loire/loiret-45?page=2
              → filtre département CÔTÉ SERVEUR via slug région/dept (vérifié :
                seuls des biens du dept demandé apparaissent). Post-filtre strict
                CP[:2] en plus (0 fuite).

Cartes : a.estate-item__link[href]  (href = URL détail, contient gbNNNNNNNN)
  - Prix : .estate-item__price / .price-for-map  →  "112 500 €"
  - Infos: .estate-item__infos  →  "Maison/villa · 5 pièces · 122m²"
  - Loc  : .estate-item__location  →  "Montargis (45200)"

Type de bien : segment "infos" (Maison/villa, Appartement...). On ne garde que
               maisons / villas / propriétés (exclut appartement / terrain...).
id_annonce : référence gbNNNNNNNN extraite de l'URL.

Pagination : ?page=N (page 1 = base sans param). On s'arrête quand une page ne
             ramène aucun id nouveau.

Couverture : réseau national, implantation concentrée en grandes villes ;
             sur la zone cible stock réel en 37/41/45/49/89, vide en 18/36/53/58/72/28
             (au moment du test). dernier_test 2026-06-09.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.lamy-immobilier.fr"
LISTING = "/acheter/acheter-un-bien/annonces-de-biens-a-vendre"
MAX_PAGES = 6


# Code département → slug "{region}/{dept-slug}" lamy-immobilier.fr
DEPT_SLUGS: dict[str, str] = {
    "18": "centre-val-de-loire/cher-18",
    "28": "centre-val-de-loire/eure-et-loir-28",
    "36": "centre-val-de-loire/indre-36",
    "37": "centre-val-de-loire/indre-et-loire-37",
    "41": "centre-val-de-loire/loir-et-cher-41",
    "45": "centre-val-de-loire/loiret-45",
    "49": "pays-de-la-loire/maine-et-loire-49",
    "53": "pays-de-la-loire/mayenne-53",
    "72": "pays-de-la-loire/sarthe-72",
    "58": "bourgogne-franche-comte/nievre-58",
    "89": "bourgogne-franche-comte/yonne-89",
}

_KEEP_TYPE = re.compile(
    r"maison|villa|propriete|propriété|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|corps-de-ferme|pavillon|grange",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"boutique|hangar|entrepot|entrepôt|viager",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, slug, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[Lamy] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Lamy] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    slug: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}{LISTING}/{slug}"
        if page > 1:
            url += f"?page={page}"
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select("a.estate-item__link")
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            try:
                bien = _parse_card(card, dept)
            except Exception:
                continue
            if not bien:
                continue

            # Post-filtre dept STRICT
            if bien["code_postal"] and bien["code_postal"][:2] != dept:
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
            biens.append(bien)
            new_on_page += 1

        if new_on_page == 0:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    href = card.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien : .estate-item__infos → "Maison/villa · 5 pièces · 122m²"
    infos_el = card.select_one(".estate-item__infos")
    infos = infos_el.get_text(" ", strip=True) if infos_el else ""
    type_raw = infos.split("·")[0].strip() if infos else ""
    if _EXCLUDE_TYPE.search(type_raw):
        return None
    if not _KEEP_TYPE.search(type_raw):
        return None
    type_bien = type_raw.lower()

    # Localisation : .estate-item__location → "Montargis (45200)"
    loc_el = card.select_one(".estate-item__location")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)

    # Prix
    price_el = card.select_one(".estate-item__price, .price-for-map")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Pièces / surface depuis infos
    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", infos)
    surface = _parse_surface(infos)

    # Référence gbNNNNNNNN
    m_ref = re.search(r"(gb\d+)", href, re.IGNORECASE)
    id_annonce = m_ref.group(1) if m_ref else url

    titre = f"{type_bien.title()} {ville}".strip()

    return {
        "source": "lamy_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": [],
        "dpe": None,
        "agence": "Lamy Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Montargis (45200)' → ('Montargis', '45200')"""
    cp = ""
    m = re.search(r"\((\d{5})\)", text)
    if m:
        cp = m.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_surface(text: str) -> float | None:
    """'Maison/villa · 5 pièces · 122m²' → 122.0"""
    m = re.search(r"([\d\s\xa0]+(?:[.,]\d+)?)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
        try:
            f = float(val)
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
    print(f"\nTotal Lamy: {len(biens)} annonces")
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

"""scrapers/demeuresdecampagne.py — Demeures de Campagne (niche prestige rural Sancerrois)

Méthode : scrape_simple (httpx) — SSR HTML (Apache, prix/villes dans le HTML brut, pas de JS).
URL pattern : /vente/{NN-nom-dept}/{page}   (ex: /vente/18-cher/1)
              → filtre département CÔTÉ SERVEUR via le préfixe NN du slug (le nom du dept
                est cosmétique : seul le numéro compte). Re-vérifié par post-filtre CP[:2].

Cartes : article.property-listing-v2__item
  - Ville : .title__content-1
  - CP    : .title__content-2     →  "(18300)"
  - Compo : .property-listing-v2__item-compo  →  "3 pièces - 67 m²"
  - Titre : h2 a.property-listing-v2__item-text > span
  - URL   : h2 a.property-listing-v2__item-text[href]
            → /vente/{NN-dept}/{ville}/{type}/{tN}/{id-slug}/
  - Prix  : .property-listing-v2__price-value  →  "69 000 €"
  - Réf   : .property-listing-v2__item-reference  →  "Ref : 685"
  - Photo : img.item__img[data-src]  (CDN //demeures-camp.staticlbi.com/...)

Type de bien : déduit du segment d'URL (1-maison, ...). On ne garde que maisons/propriétés.

Couverture : portail mono-réseau spécialisé Sancerrois — stock concentré sur le Cher (18)
             et la Nièvre (58). Sur les départements cibles éloignés (72/28/45/89) : 0 stock
             (slug accepté mais aucune annonce). Scraper conservé, réactiver si implantation.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.demeuresdecampagne.com"
MAX_PAGES = 10
PHOTOS_PER_CARD = 10


# Code département → slug URL /vente/{NN-nom}/{page} (nom cosmétique, seul NN filtre)
DEPT_SLUGS: dict[str, str] = {
    "72": "sarthe",
    "28": "eure-et-loir",
    "45": "loiret",
    "89": "yonne",
    "49": "maine-et-loire",
    "37": "indre-et-loire",
    "36": "indre",
    "18": "cher",
    "58": "nievre",
    "41": "loir-et-cher",
    "53": "mayenne",
}

# Types de bien (segment d'URL) à conserver : maisons / propriétés / fermes...
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|presbytere|presbytère|"
    r"corps-de-ferme|maison-de-village|fermette",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
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
                print(f"[DemeuresCampagne] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[DemeuresCampagne] Erreur dept {dept}: {e}")
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
        url = f"{BASE_URL}/vente/{dept}-{slug}/{page}"
        r = await client.get(url)
        if r.status_code != 200:
            break
        # Garde-fou : si le serveur a redirigé hors du slug dept (ex. vers l'accueil)
        if f"/vente/{dept}-" not in str(r.url):
            break

        cards = BeautifulSoup(r.text, "html.parser").select(
            "article.property-listing-v2__item"
        )
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

            # Post-filtre dept STRICT (0 fuite hors-zone)
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
    link = card.select_one("h2 a.property-listing-v2__item-text") or card.select_one(
        "a.property-listing-v2__item-text"
    )
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL : /vente/{NN-dept}/{ville}/{type}/{tN}/{id-slug}/
    parts = [p for p in href.split("/") if p]
    # parts: ['vente', '18-cher', '1-sancerre', '1-maison', 't3', '360-...']
    type_seg = parts[3] if len(parts) > 3 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean) and not _KEEP_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        return None
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # Référence (id_annonce)
    ref_el = card.select_one(".property-listing-v2__item-reference")
    ref_txt = ref_el.get_text(" ", strip=True) if ref_el else ""
    m_ref = re.search(r"Ref\s*:?\s*([\w\-]+)", ref_txt, re.IGNORECASE)
    ref = m_ref.group(1) if m_ref else ""
    id_num = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_num = m.group(1)
    id_annonce = ref or id_num or url

    # Localisation : ville + "(CODEPOSTAL)"
    ville_el = card.select_one(".title__content-1")
    ville = ville_el.get_text(" ", strip=True) if ville_el else ""
    cp_el = card.select_one(".title__content-2")
    cp_txt = cp_el.get_text(" ", strip=True) if cp_el else ""
    m_cp = re.search(r"(\d{5})", cp_txt)
    code_postal = m_cp.group(1) if m_cp else ""
    # Secours : CP introuvable dans la carte → on n'invente pas, le post-filtre gère

    # Titre
    title_span = link.select_one("span") if link else None
    titre = (title_span or link).get_text(" ", strip=True) if link else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Prix
    price_el = card.select_one(".property-listing-v2__price-value")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Composition : "3 pièces - 67 m²"
    compo_el = card.select_one(".property-listing-v2__item-compo")
    compo = compo_el.get_text(" ", strip=True) if compo_el else ""
    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", compo)
    surface = _parse_surface(compo)
    # Pièces en secours : segment tN de l'URL
    if pieces is None and len(parts) > 4:
        m = re.match(r"^t(\d+)$", parts[4])
        if m:
            pieces = int(m.group(1))

    # Photos
    photos = []
    for img in card.select("img.item__img"):
        src = img.get("data-src") or img.get("data-path") or ""
        if not src or src.startswith("data:"):
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif not src.startswith("http"):
            src = "https://demeures-camp.staticlbi.com/original/images/" + src.lstrip(
                "/"
            )
        photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "demeuresdecampagne",
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
        "photos": photos,
        "dpe": None,
        "agence": "Demeures de Campagne",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_surface(text: str) -> float | None:
    """'3 pièces - 67 m²' → 67.0"""
    m = re.search(r"([\d\s\xa0]+)\s*m²", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 3000:
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
    print(f"\nTotal Demeures de Campagne: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

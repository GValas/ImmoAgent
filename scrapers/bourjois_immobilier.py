"""scrapers/bourjois_immobilier.py — Bourjois Immobilier (agence indépendante Sens, Yonne 89)

Méthode : scrape_simple (httpx) — SSR HTML (Apache, moteur LBI / staticlbi.com)
URL pattern : /vente/{NN-dept-slug}/{page}   (ex: /vente/89-yonne/1)
              → filtre département CÔTÉ SERVEUR par le slug d'URL (vérifié : aucune
              fuite hors-dept ; un dept non couvert renvoie une page 200 vide).
              On peut aussi cibler ville/type (/vente/89-yonne/1-sens/1-maison/N)
              mais on liste tous types au niveau département et on filtre en Python.

Cartes : div.property-listing-v1__item.item   (~10/page)
  - URL   : a.item__link[href] (ou .js-obfuscation[data-url])
            → /vente/{NN-dept}/{ville}/{type}/{tN}/{id-slug}/
  - Loc   : .title-subtitle__subtitle  →  "Ville (CODEPOSTAL)"
  - Titre : .title-subtitle__content
  - Réf   : .item__info-id  →  "Réf : 14565"
  - Extra : .item__info-extra  →  "214 m² - 197 000 €"  (surface habitable + prix)
  - Prix  : .__price-value     →  "197 000 €"
  - Photos: img.item__img[data-src]   (// → https:)

Type de bien : déduit du segment d'URL (1-maison, 2-appartement, 4-studio, tN pour
               le nb de pièces). On ne garde que maisons / propriétés / fermes…

Couverture : agence mono-département (Yonne 89, secteur Sens). Stock réel sur 89,
             0 bien sur les autres départements cibles (URL renvoie page vide).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.bourjois-immobilier.fr"
MAX_PAGES = 12
PHOTOS_PER_CARD = 10


# Code département → slug URL bourjois-immobilier.fr/vente/{NN-slug}/{page}
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
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|maison-de-village|"
    r"pavillon",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|studio",
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
                print(f"[Bourjois] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Bourjois] Erreur dept {dept}: {e}")
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

        cards = BeautifulSoup(r.text, "html.parser").select(
            "div.property-listing-v1__item.item"
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

            # Sécurité : on n'accepte que le département cible (filtre serveur déjà OK)
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
    link = card.select_one("a.item__link")
    href = link.get("href", "") if link else ""
    if not href:
        obf = card.select_one(".js-obfuscation")
        href = obf.get("data-url", "") if obf else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL :
    # /vente/{NN-dept}/{ville}/{type}/{tN}/{id-slug}/
    parts = [p for p in href.split("/") if p]
    # parts: ['vente', '89-yonne', '49-les-bordes', '1-maison', 't9', '6943-...']
    type_seg = parts[3] if len(parts) > 3 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean) and not _KEEP_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        # type inconnu/ambigu → on exclut par prudence
        return None
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # Pièces depuis le segment tN
    pieces = None
    for seg in parts:
        m = re.match(r"^t(\d+)$", seg)
        if m:
            pieces = int(m.group(1))
            break

    # Référence (id_annonce)
    ref_el = card.select_one(".item__info-id")
    ref_txt = ref_el.get_text(" ", strip=True) if ref_el else ""
    m_ref = re.search(r"R[ée]f\s*:?\s*([A-Za-z0-9\-]+)", ref_txt)
    ref = m_ref.group(1) if m_ref else ""
    # id numérique du slug final en secours
    id_num = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_num = m.group(1)
    id_annonce = ref or id_num or url

    # Localisation : "Ville (CODEPOSTAL)"
    sub_el = card.select_one(".title-subtitle__subtitle")
    loc = sub_el.get_text(" ", strip=True) if sub_el else ""
    ville, code_postal = _parse_loc(loc)
    # secours ville depuis le slug d'URL si subtitle vide
    if not ville and len(parts) > 2:
        ville = re.sub(r"^\d+-", "", parts[2]).replace("-", " ").title()

    # Titre
    title_el = card.select_one(".title-subtitle__content")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Extra : "214 m² - 197 000 €"  → surface habitable
    extra_el = card.select_one(".item__info-extra")
    extra_txt = extra_el.get_text(" ", strip=True) if extra_el else ""
    surface = _parse_surface_hab(extra_txt)

    # Prix
    price_el = card.select_one(".__price-value")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else extra_txt)

    # Photos
    photos = []
    for img in card.select("img.item__img"):
        src = img.get("data-src") or img.get("data-path") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "bourjois_immobilier",
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
        "agence": "Bourjois Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Bordes (89500)' → ('Bordes', '89500')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_surface_hab(text: str) -> float | None:
    """'214 m² - 197 000 €' → 214.0 (premier 'NNN m²' = surface habitable)."""
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0]*)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
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
    print(f"\nTotal Bourjois Immobilier: {len(biens)} annonces")
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

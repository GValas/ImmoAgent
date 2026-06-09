"""scrapers/globalimmobilier.py — Global Immobilier (réseau de mandataires)

Méthode : scrape_simple (httpx) — SSR HTML (pas de JS nécessaire)
Site     : https://www.globalimmobilier.immo
URL pattern : /vente/{NN-dept-slug}/{page}   (ex: /vente/85-vendee/1)
              → filtre département CÔTÉ SERVEUR via le slug. La page d'un
              département sans stock renvoie un gabarit court (~121 ko) sans
              cartes ; une page avec stock contient des <article>. Vérifié :
              aucune fuite hors-département dans les cartes (toutes les villes
              listées appartiennent bien au département du slug).

Cartes : article.property-listing-v2__container.item
  - URL/Titre : a.item__title[href]  → /vente/{NN-dept}/{ville}/{type}/{tN}/{id-slug}/
                .title__content-1  →  "Ville (CODEPOSTAL)"
                .title__content-2  →  libellé / accroche (sert de description)
  - Prix  : .item__price          →  "115 000 €"
  - Réf   : .item__reference       →  "Réf : 8086"
  - Photos: img[src] (CDN //global-immobilier.staticlbi.com/...)

Type de bien & nb de pièces : déduits du slug d'URL (/1-maison/t7/...). La carte
ne porte PAS la surface habitable ni le terrain (présents seulement sur la page
détail) → surface/surface_terrain/chambres restent None au niveau liste.

Couverture : réseau à implantation très inégale. Sur la zone cible le stock est
             marginal (89 et 53 ont quelques biens ; les autres départements 0)
             mais le scraper est fonctionnel et le filtre département fiable.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.globalimmobilier.immo"
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

# Code département → slug URL /vente/{NN-slug}/{page}
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
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|maison-de-village",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"viager",
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
                print(f"[GlobalImmo] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[GlobalImmo] Erreur dept {dept}: {e}")
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
            "article.property-listing-v2__container.item"
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

            # Post-filtre département STRICT (objectif : 0 fuite hors-zone)
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
    link = card.select_one("a.item__title")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Slug : /vente/{NN-dept}/{ville-id}/{type}/{tN}/{id-slug}/
    parts = [p for p in href.split("/") if p]
    # parts ≈ ['vente', '85-vendee', '17172-benet', '1-maison', 't7', '24470-...']
    type_seg = parts[3] if len(parts) > 3 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean) and not _KEEP_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        # type inconnu/ambigu → on exclut par prudence
        return None
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # Nombre de pièces depuis le segment tN
    pieces = None
    if len(parts) > 4:
        m = re.match(r"^t(\d+)$", parts[4])
        if m:
            pieces = int(m.group(1))

    # id_annonce : référence affichée, sinon id numérique du dernier segment
    ref_el = card.select_one(".item__reference")
    ref = ""
    if ref_el:
        m = re.search(r"(\d+)", ref_el.get_text(strip=True))
        ref = m.group(1) if m else ""
    id_num = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_num = m.group(1)
    id_annonce = ref or id_num or url

    # Localisation : .title__content-1 = "Ville (CODEPOSTAL)"
    loc_el = card.select_one(".title__content-1")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)

    # Accroche / libellé : .title__content-2
    desc_el = card.select_one(".title__content-2")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    titre = f"{type_bien.title()} {ville}".strip()
    if description:
        titre = f"{titre} — {description}"

    # Prix : .item__price → "115 000 €"
    price_el = card.select_one(".item__price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Surface (parfois mentionnée dans l'accroche)
    surface = _parse_surface_hab(description)

    # Photos (CDN ; URLs en // → https:)
    photos = []
    for img in card.find_all("img"):
        src = img.get("data-src") or img.get("data-original") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "globalimmobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
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
        "agence": "Global Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Benet (85490)' → ('Benet', '85490')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface_hab(text: str) -> float | None:
    """Cherche 'NNN m²' dans le texte libre (accroche)."""
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0]*)\s*m[²2]", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 2000:
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
    print(f"\nTotal Global Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

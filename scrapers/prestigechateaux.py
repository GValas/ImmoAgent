"""scrapers/prestigechateaux.py — Prestige & Châteaux (ericmeyprestige / châteaux & demeures de prestige)

Méthode : scrape_simple (httpx) — SSR HTML (Apache, pas de Cloudflare)
URL pattern : /vente/{NN-dept-slug}/{page}   (ex: /vente/2a-corse-du-sud/1)
              → filtre département CÔTÉ SERVEUR FIABLE (slug d'URL ; vérifié : aucune fuite).

Cartes : div.property-listing-v3__item.item
  - URL   : a[href^="/vente/"]            → /vente/{NN-dept}/{ville}/{type}/{tN}/{id-slug}/
  - Loc   : .title-subtitle__subtitle     → "Ville (CODEPOSTAL)" (CP après <br/>)
  - Titre : .title-subtitle__content
  - Extra : .item__info-extra             → "280 m²" et "<span class='__price-value'>1 768 000 €</span>"
  - Texte : .item__text-block (description)
  - Réf   : .item__info-id                → "Réf : PVVI..."
  - Photos: img.item__img[data-src]       → "//ericmeyprestige.staticlbi.com/..."

Type de bien : déduit du segment d'URL (1-maison, 25-villa, 22-propriete,
               20-autre, chateau...). On ne garde que maisons/propriétés/châteaux/villas.

Couverture : spécialiste prestige, concentration Corse / PACA / Charente.
             Sur les départements cibles (Val-de-Loire / Ouest) : 0 stock observé
             (45,41,37,18,58,49,72,28,89,53,36 testés le 2026-06-09).
             Scraper conservé et fonctionnel ; réactiver si implantation dans la zone.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.prestigechateaux.com"
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

# Code département → slug URL prestigechateaux.com/vente/{NN-slug}/{page}
DEPT_SLUGS: dict[str, str] = {
    "72": "72-sarthe",
    "28": "28-eure-et-loir",
    "45": "45-loiret",
    "89": "89-yonne",
    "49": "49-maine-et-loire",
    "37": "37-indre-et-loire",
    "36": "36-indre",
    "18": "18-cher",
    "58": "58-nievre",
    "41": "41-loir-et-cher",
    "53": "53-mayenne",
}

# Types de bien (segment d'URL) à conserver : maisons / propriétés / châteaux...
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|hotel-particulier|bord-de-mer|autre",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|loft",
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
                print(f"[PrestigeChateaux] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[PrestigeChateaux] Erreur dept {dept}: {e}")
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
        url = f"{BASE_URL}/vente/{slug}/{page}"
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select(
            "div.property-listing-v3__item"
        )
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            try:
                bien = _parse_card(card, dept, slug)
            except Exception:
                continue
            if not bien:
                continue

            # Sécurité : filtre serveur déjà OK, on revérifie le département
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


def _parse_card(card, dept: str, slug: str) -> dict | None:
    link = card.select_one('a[href^="/vente/"]')
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL : /vente/{NN-dept}/{ville}/{type}/{tN}/{id-slug}/
    parts = [p for p in href.split("/") if p]
    type_seg = parts[3] if len(parts) > 3 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean) and not _KEEP_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        return None
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # Référence (id_annonce)
    ref_el = card.select_one(".item__info-id")
    ref = ""
    if ref_el:
        ref = re.sub(r"^\s*R[ée]f\s*:\s*", "", ref_el.get_text(strip=True), flags=re.I)
    id_num = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_num = m.group(1)
    id_annonce = ref or id_num or url

    # Localisation : "Ville (CODEPOSTAL)" (CP après un <br/>)
    sub_el = card.select_one(".title-subtitle__subtitle")
    loc = sub_el.get_text(" ", strip=True) if sub_el else ""
    ville, code_postal = _parse_loc(loc)

    # Titre
    title_el = card.select_one(".title-subtitle__content")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Description
    text_el = card.select_one(".item__text-block")
    description = text_el.get_text(" ", strip=True) if text_el else ""

    # Prix
    price_el = card.select_one(".__price-value")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Surface : "280 m²" dans un .item__info-extra (hors prix)
    surface = None
    for ex in card.select(".item__info-extra"):
        if ex.select_one(".__price-value"):
            continue
        m = re.search(r"(\d[\d\s\xa0]*)\s*m²", ex.get_text(" ", strip=True))
        if m:
            val = re.sub(r"[\s\xa0]", "", m.group(1))
            try:
                f = float(val)
                if 8 <= f <= 5000:
                    surface = f
                    break
            except ValueError:
                pass

    # Pièces : segment tN de l'URL
    pieces = None
    if len(parts) > 4:
        m = re.match(r"^t(\d+)$", parts[4])
        if m:
            pieces = int(m.group(1))

    # Photos
    photos = []
    for img in card.select("img.item__img"):
        src = img.get("data-src") or img.get("data-lazy") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "prestigechateaux",
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
        "agence": "Prestige & Châteaux",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Porticcio (20166)' → ('Porticcio', '20166')"""
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
    print(f"\nTotal Prestige & Châteaux: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

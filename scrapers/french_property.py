"""scrapers/french_property.py — French Property (french-property.com)

Portail acheteurs anglophones, nombreux biens ruraux français.

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /properties-for-sale?departments={slug}&start_page=N
  ATTENTION : le slug doit être en minuscule (ex: "sarthe", "eure-et-loir").
              Une majuscule (?departments=Sarthe) renvoie une page non filtrée.

Filtre département : côté serveur, FIABLE. Vérifié — toutes les cartes d'un dept
donné portent bien le bon code dans ".location_full" → "... Sarthe (72), Ville".
Post-filtre de sécurité par code dept extrait de la carte.

Deux types de cartes coexistent :
  - featured : li.property_listing[itemtype*=ListItem]  (peu de champs)
  - standard : li.property_listing.standard avec div[itemprop=item] (riche)

Pagination : ?start_page=N (25 résultats/page). Le total est dans "Results A - B of T".

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.french-property.com"
PHOTOS_PER_CARD = 10
MAX_PAGES = 8  # 25/page → plafond ~200 biens/dept

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Code département → slug URL french-property.com (vérifié via index Algolia LOCATIONS)
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

# Exclure les annonces qui ne sont pas des maisons/propriétés
_EXCLUDE_KEYWORDS = re.compile(
    r"\bapartment\b|\bappartement\b|\bstudio\b|building plot|\bland\b for sale|"
    r"\bgarage\b|parking|commercial|business for sale|\bshop\b|\boffice\b",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0) or 0
    prix_min = criteres.get("prix_min", 0) or 0
    surface_min = criteres.get("surface_min", 0) or 0

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
                print(f"[FrenchProperty] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[FrenchProperty] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.8)

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
    total_pages = MAX_PAGES

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/properties-for-sale?departments={slug}"
        if page > 1:
            url += f"&start_page={page}"

        r = await client.get(url)
        r.raise_for_status()
        html = r.text

        # Total de pages depuis "Results A - B of T" (25/page)
        if page == 1:
            m = re.search(r"Results\s+[\d,]+\s*-\s*([\d,]+)\s+of\s+([\d,]+)", html)
            if m:
                per_page = int(m.group(1).replace(",", "")) or 25
                total = int(m.group(2).replace(",", ""))
                if per_page > 0:
                    total_pages = min(MAX_PAGES, -(-total // per_page))

        page_biens = _parse_html(html, dept, prix_max, prix_min, surface_min)

        new_count = 0
        for b in page_biens:
            aid = b.get("id_annonce") or b.get("url", "")
            if aid and aid not in seen_ids:
                seen_ids.add(aid)
                biens.append(b)
                new_count += 1

        if page >= total_pages or new_count == 0:
            break

        await asyncio.sleep(0.6)

    return biens


def _parse_html(
    html: str,
    dept: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []

    for card in soup.select("li.property_listing"):
        try:
            bien = _parse_card(card, dept)
            if not bien:
                continue

            # Post-filtre département FIABLE : le code extrait doit matcher
            if bien["departement"] != dept:
                continue

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            results.append(bien)
        except Exception:
            continue

    return results


def _parse_card(card, dept: str) -> dict | None:
    # ── URL / référence ──────────────────────────────────────────────────
    url = ""
    meta_url = card.select_one("meta[itemprop=url]")
    if meta_url and meta_url.get("content"):
        url = meta_url["content"]
    if not url:
        a = card.select_one("a[href*='/sale-property/']")
        if a:
            url = a.get("href", "")
    if not url:
        return None
    url = url.split("?")[0]
    if not url.startswith("http"):
        url = BASE_URL + url

    # id_annonce : depuis l'URL /sale-property/{id}
    m_id = re.search(r"/sale-property/([^/?#]+)", url)
    id_annonce = m_id.group(1) if m_id else None

    # Référence agence (productID) si présente
    ref_el = card.select_one("span[itemprop=productID]")
    if ref_el:
        ref_txt = re.sub(r"(?i)ref\s*:?\s*", "", ref_el.get_text(strip=True)).strip()
        if ref_txt:
            id_annonce = id_annonce or ref_txt

    # ── Localisation : ".location_full" = "Region, Dept (NN), Ville" ──────
    loc_el = card.select_one(".location_full")
    loc_text = loc_el.get_text(" ", strip=True) if loc_el else ""
    loc_text = re.sub(r"(?i)^location\s*:?\s*", "", loc_text).strip()

    # Département extrait du "(NN)" — filtre de sécurité
    m_dep = re.search(r"\((\d{2,3})\)", loc_text)
    dep_card = m_dep.group(1).zfill(2)[:2] if m_dep else ""
    if not dep_card:
        # fallback : map SVG "#department-NN"
        use = card.select_one("use.map-departments")
        if use:
            href = use.get("xlink:href") or use.get("href") or ""
            m2 = re.search(r"department-(\d{2,3})", href)
            if m2:
                dep_card = m2.group(1).zfill(2)[:2]
    if not dep_card:
        dep_card = dept  # si introuvable, on fait confiance au filtre serveur

    # Ville : dernier segment de location_full, sinon .commune
    ville = ""
    code_postal = ""
    commune_el = card.select_one(".location_details .commune strong")
    if commune_el:
        commune_txt = commune_el.get_text(" ", strip=True)
        m_cp = re.search(r"(\d{5})", commune_txt)
        if m_cp:
            code_postal = m_cp.group(1)
        ville = re.sub(r",?\s*\d{5}.*$", "", commune_txt).strip()
    if not ville and loc_text:
        parts = [p.strip() for p in loc_text.split(",")]
        if parts:
            ville = parts[-1]

    # ── Titre ────────────────────────────────────────────────────────────
    titre = ""
    h3 = card.select_one("h3[itemprop=name] a, h3 a")
    if h3:
        titre = h3.get("title", "").strip() or h3.get_text(" ", strip=True)
    if not titre:
        link = card.select_one("a[title]")
        if link:
            titre = link.get("title", "").strip()

    # Exclusion type de bien (appartements, terrains, locaux…)
    blob = f"{titre} {url}"
    if _EXCLUDE_KEYWORDS.search(blob):
        return None

    # ── Description ──────────────────────────────────────────────────────
    desc_el = card.select_one(".description[itemprop=description], .description")
    description = desc_el.get_text(" ", strip=True)[:1200] if desc_el else ""

    # ── Prix : meta[itemprop=price] (brut), sinon .price h4 ───────────────
    prix = None
    meta_price = card.select_one("meta[itemprop=price]")
    if meta_price and meta_price.get("content"):
        prix = _to_float(meta_price["content"])
    if prix is None:
        price_el = card.select_one(".price h4")
        if price_el:
            prix = _parse_price(price_el.get_text(strip=True))

    # ── Caractéristiques ─────────────────────────────────────────────────
    chambres = _info_int(card, ".info-beds strong")
    if chambres is None:
        # carte featured : ".info-content" = nb de chambres (icône lit)
        ic = card.select_one(".info-content")
        if ic:
            chambres = _to_int(ic.get_text(strip=True))

    surface = _info_m2(card, ".info-habitable strong")
    surface_terrain = _info_m2(card, ".info-land strong")

    # ── Photos : meta[itemprop=contentUrl] (haute déf) sinon data-src ─────
    photos: list[str] = []
    for meta in card.select("meta[itemprop=contentUrl]"):
        c = meta.get("content")
        if c and c.startswith("http"):
            photos.append(c)
    if not photos:
        for img in card.select("img[data-src]"):
            src = img.get("data-src", "").strip()
            if src.startswith("http"):
                photos.append(src)
    # dédup en gardant l'ordre
    seen: set[str] = set()
    photos = [p for p in photos if not (p in seen or seen.add(p))][:PHOTOS_PER_CARD]

    if not titre:
        titre = f"Propriété {ville}".strip() or "Propriété French-Property"

    return {
        "source": "french_property",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": description,
        "departement": dep_card,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "French-Property",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _to_float(text: str) -> float | None:
    cleaned = re.sub(r"[^\d.]", "", str(text).replace(",", ""))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _to_int(text: str) -> int | None:
    m = re.search(r"\d+", str(text).replace(",", ""))
    return int(m.group(0)) if m else None


def _parse_price(text: str) -> float | None:
    """'€840,000' → 840000.0"""
    cleaned = re.sub(r"[^\d]", "", str(text))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _info_int(card, selector: str) -> int | None:
    el = card.select_one(selector)
    return _to_int(el.get_text(" ", strip=True)) if el else None


def _info_m2(card, selector: str) -> float | None:
    """'350 m²' ou '2,130 m²' → float (m²)"""
    el = card.select_one(selector)
    if not el:
        return None
    txt = el.get_text(" ", strip=True)
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*m", txt)
    if m:
        return _to_float(m.group(1))
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
                "departements": criteres.departements[:4],
                "prix_max": criteres.prix_max,
                "prix_min": getattr(criteres, "prix_min", 0),
                "surface_min": criteres.surface_min,
            }
        )
    )
    print(f"\nTotal French-Property: {len(biens)} annonces")
    from collections import Counter

    by_dep = Counter(b["departement"] for b in biens)
    print("Par département:", dict(by_dep))
    for b in biens[:8]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface', '?')}m²"
            f" — {b.get('surface_terrain', '?')}m² terrain"
            f" — {b['ville']} {b.get('code_postal', '')}"
        )

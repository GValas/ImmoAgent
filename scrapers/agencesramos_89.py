"""scrapers/agencesramos_89.py — Agences Ramos (agence indépendante, Auxerre 89)

Méthode : scrape_simple (httpx) — SSR HTML
Site : agence immobilière indépendante (depuis 1973) implantée dans l'Yonne (89)
       et la Nièvre (58), couvrant aussi quelques biens limitrophes (18, 45...).

URL pattern (catégories, SSR, pas de pagination — tout sur une page) :
  - /fr/page/les-maisons             → maisons
  - /fr/page/autres-types-de-biens   → propriétés / pavillons / divers

⚠️ Les pages /fr/page/achat-maison/{ville} sont des landings SEO SANS annonces ;
   les vraies annonces sont sur les pages de catégorie ci-dessus.

Cartes : article.item (a.animation-link)
  - URL    : a.animation-link[href]  → /fr/product/maison/maison-de-102-m2-89110-poilly-sur-tholon
             Le slug encode SURFACE (…-de-{N}-m2-…), CODE POSTAL (5 chiffres) et VILLE.
  - Image  : img.card-img-top[src]
  - Catég. : span.category   → "Maison"
  - Titre  : h5.title
  - Prix   : div.price        → "182.000 €"

Filtre département : PAS de filtre serveur fiable (agence locale mono-zone) →
  post-filtre STRICT sur code_postal[:2] (extrait du slug d'URL) ⊆ départements
  cibles. La page mélange surtout du 89/58 plus quelques limitrophes.

Détail (best-effort) : la page produit donne terrain / DPE / chambres / pièces
  (récupérés sur un nombre limité de biens pour rester poli).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://agencesramos.com"
LISTING_PATHS = [
    "/fr/page/les-maisons",
    "/fr/page/autres-types-de-biens",
]
# Nb max de pages détail enrichies (politesse / rapidité)
MAX_DETAIL = 60
PHOTOS_PER_CARD = 8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Départements cibles (post-filtre strict)
_TARGET_DEPTS = {"72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"}

# Types de bien à conserver (catégorie + slug)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    # On reste de toute façon borné aux départements cibles connus du projet
    departements &= _TARGET_DEPTS
    if not departements:
        departements = _TARGET_DEPTS

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        cards: list[dict] = []
        for path in LISTING_PATHS:
            try:
                cards.extend(await _scrape_listing(client, path))
            except Exception as e:
                print(f"[AgencesRamos89] Erreur listing {path}: {e}")
            await asyncio.sleep(0.5)

        detail_budget = MAX_DETAIL
        for bien in cards:
            dept = bien["code_postal"][:2] if bien["code_postal"] else ""

            # Post-filtre département STRICT — 0 fuite hors-zone
            if dept not in departements:
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

            bien["departement"] = dept

            # Enrichissement page détail (terrain / DPE / pièces / photos)
            if detail_budget > 0:
                try:
                    await _enrich_detail(client, bien)
                    detail_budget -= 1
                    await asyncio.sleep(0.4)
                except Exception:
                    pass

            seen_ids.add(bien["id_annonce"])
            results.append(bien)

    print(f"[AgencesRamos89] Total: {len(results)} annonces (zone cible)")
    return results


async def _scrape_listing(client: httpx.AsyncClient, path: str) -> list[dict]:
    r = await client.get(BASE_URL + path)
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    out: list[dict] = []
    for art in soup.select("article.item, article"):
        link = art.select_one("a.animation-link")
        if not link:
            continue
        bien = _parse_card(art, link)
        if bien:
            out.append(bien)
    return out


def _parse_card(art, link) -> dict | None:
    href = link.get("href", "")
    if not href or "/product/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    cat_el = art.select_one(".category")
    categorie = cat_el.get_text(strip=True) if cat_el else ""

    # On exclut appartements / terrains (slug + catégorie)
    if _EXCLUDE_TYPE.search(categorie) or _EXCLUDE_TYPE.search(href):
        return None

    # Code postal depuis le slug : …-89110-poilly-sur-tholon
    cp = ""
    m_cp = re.search(r"-(\d{5})-", href)
    if m_cp:
        cp = m_cp.group(1)

    # Surface depuis le slug : …-de-102-m2-… ou …-de-86-6-m2-… (86,6 m²)
    surface = None
    m_s = re.search(r"-de-(\d+(?:-\d+)?)-m2-", href)
    if m_s:
        surface = _to_float(m_s.group(1).replace("-", "."))

    # Ville depuis le slug (après le CP)
    ville = ""
    if cp:
        m_v = re.search(rf"-{cp}-(.+)$", href)
        if m_v:
            ville = m_v.group(1).replace("-", " ").strip().title()

    title_el = art.select_one("h5.title, .title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    price_el = art.select_one(".price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    photos = []
    img = art.select_one("img.card-img-top, .image img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(src if src.startswith("http") else BASE_URL + src)

    type_bien = categorie.lower() or "maison"
    id_annonce = href.rstrip("/").split("/")[-1]

    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    return {
        "source": "agencesramos_89",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": cp[:2] if cp else "",
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Agences Ramos",
    }


async def _enrich_detail(client: httpx.AsyncClient, bien: dict) -> None:
    r = await client.get(bien["url"])
    if r.status_code != 200:
        return
    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True)

    # Description (best-effort)
    desc_el = soup.select_one(".description, .content, .product-description, article")
    if desc_el:
        desc = desc_el.get_text(" ", strip=True)
        if len(desc) > len(bien.get("description") or ""):
            bien["description"] = desc[:1200]

    # Terrain : "Terrain de 1403 m²" / "terrain : 1403"
    m_t = re.search(r"terrain[^0-9]{0,12}(\d[\d\s\xa0]*)\s*m", text, re.IGNORECASE)
    if m_t:
        bien["surface_terrain"] = _to_float(re.sub(r"[\s\xa0]", "", m_t.group(1)))

    # Chambres / pièces
    m_ch = re.search(r"(\d+)\s*chambre", text, re.IGNORECASE)
    if m_ch:
        bien["chambres"] = int(m_ch.group(1))
    m_pc = re.search(r"(\d+)\s*pi[eè]ce", text, re.IGNORECASE)
    if m_pc:
        bien["pieces"] = int(m_pc.group(1))

    # DPE : "Consomation énergétique : C" / "DPE : D" / "classe énergie D"
    m_dpe = re.search(
        r"(?:conso\w*\s+énerg\w*|DPE|classe\s+énerg\w*)\s*[:\-]?\s*([A-G])\b",
        text,
        re.IGNORECASE,
    )
    if m_dpe:
        bien["dpe"] = m_dpe.group(1).upper()

    # Photos additionnelles
    for img in soup.select(".gallery img, .slider img, .swiper img, img.card-img-top"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            full = src if src.startswith("http") else BASE_URL + src
            if full not in bien["photos"]:
                bien["photos"].append(full)
    bien["photos"] = bien["photos"][:PHOTOS_PER_CARD]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    # "182.000 €" → 182000  ;  "1 250 000 €" → 1250000
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = cleaned.replace(".", "").replace(",", "")
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _to_float(s: str) -> float | None:
    try:
        return float(s)
    except (ValueError, TypeError):
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
    print(f"\nTotal Agences Ramos: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — DPE {b.get('dpe') or '?'}"
            f" — {b['type_bien']} — {b['ville']}"
        )

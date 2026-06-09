"""scrapers/fnaim_beugnot.py — Cabinet Beugnot (agence indépendante FNAIM, Nevers / Nièvre 58)

Méthode : scrape_simple (httpx) — SSR HTML (Apache, pas de JS, pas de Cloudflare).

URL pattern : /vente/{NN-dept-slug}/{page}   (ex: /vente/58-nievre/1)
  → filtre département CÔTÉ SERVEUR. La pagination est un simple entier en fin
    d'URL. ⚠ Important : le site exige une SESSION établie (cookie) pour servir
    les pages > 1 ; un AsyncClient persistant avec un petit délai entre pages
    suffit (sinon page 2+ renvoie 0 carte). On post-filtre quand même CP[:2].

Cartes : article.property-listing-v2__item
  - URL    : a.property-listing-v2__item-text[href]  (ou data-url du wrapper)
             /vente/{NN-dept}/{ville}/{type}/{tN}/{id-slug}/
  - Ville  : span.title__content-1   →  "Nevers"
  - CP     : span.title__content-2   →  "(58000)"
  - Compo  : .property-listing-v2__item-compo  →  "5 pièces - 144 m²"
  - Titre  : h2 a span
  - Prix   : .property-listing-v2__price-value  →  "230 000 €"
  - Réf    : .property-listing-v2__item-reference  →  "Ref : 13356"
  - Photo  : img.item__img[data-src]  (protocol-relative //cabbeugnot.staticlbi.com)

Type de bien : déduit du segment d'URL ({1-maison, 22-propriete, 41-triplex,
               2-appartement...}). On ne garde que maisons / propriétés.

Couverture : agence mono-implantation (Nevers). Stock RÉEL uniquement en Nièvre
             (58) — ~24 maisons/propriétés sur 3 pages. Les autres slugs dept
             (72/28/45/89...) répondent 200 mais 0 carte (aucune fuite).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.fnaim-beugnot.com"
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

# Types de bien (segment d'URL) à conserver : maisons / propriétés...
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|pavillon|"
    r"maison-de-village",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|triplex|duplex|studio|terrain|local|commerce|garage|parking|"
    r"immeuble|bureau|fonds|professionnel",
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
                print(f"[Beugnot] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Beugnot] Erreur dept {dept}: {e}")
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

            # Filtre dept STRICT (le slug filtre déjà côté serveur, on revérifie)
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
    link = card.select_one("a.property-listing-v2__item-text")
    href = link.get("href", "") if link else ""
    if not href:
        wrapper = card.select_one("[data-url]")
        href = wrapper.get("data-url", "") if wrapper else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL : /vente/{NN-dept}/{ville}/{type}/{tN}/{id-slug}/
    parts = [p for p in href.split("/") if p]
    type_seg = parts[3] if len(parts) > 3 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        return None
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # Référence (id_annonce)
    ref_el = card.select_one(".property-listing-v2__item-reference")
    ref_text = ref_el.get_text(" ", strip=True) if ref_el else ""
    m_ref = re.search(r"Ref\s*:?\s*(\S+)", ref_text)
    ref = m_ref.group(1) if m_ref else ""
    # id numérique du slug final en secours
    id_num = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_num = m.group(1)
    id_annonce = ref or id_num or url

    # Localisation : ville + CP dans deux spans séparés
    ville_el = card.select_one(".title__content-1")
    cp_el = card.select_one(".title__content-2")
    ville = ville_el.get_text(" ", strip=True) if ville_el else ""
    cp_raw = cp_el.get_text(" ", strip=True) if cp_el else ""
    m_cp = re.search(r"(\d{5})", cp_raw)
    code_postal = m_cp.group(1) if m_cp else ""

    # Titre
    title_el = card.select_one("h2")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Compo : "5 pièces - 144 m²"
    compo_el = card.select_one(".property-listing-v2__item-compo")
    compo = compo_el.get_text(" ", strip=True) if compo_el else ""
    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", compo)
    surface = _parse_surface(compo)

    # Prix
    price_el = card.select_one(".property-listing-v2__price-value")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Photos
    photos = []
    for img in card.select("img.item__img"):
        src = img.get("data-src") or img.get("data-path") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "fnaim_beugnot",
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
        "agence": "Cabinet Beugnot",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_surface(text: str) -> float | None:
    """'5 pièces - 144 m²' → 144.0 (surface habitable)."""
    m = re.search(r"([\d\s\xa0]+)\s*m²", text)
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
    print(f"\nTotal Cabinet Beugnot: {len(biens)} annonces")
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

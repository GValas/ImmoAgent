"""scrapers/immo_entre_particuliers.py — Immo entre Particuliers (P2P, sans frais d'agence)

Méthode : scrape_simple (httpx) — SSR HTML (Apache, pas de Cloudflare/JS)

Filtre département CÔTÉ SERVEUR via SOUS-DOMAINE par département :
  https://{dept-slug}.immo-entre-particuliers.com/annonces/vente/maison
  → 0 fuite vérifiée (tous les CP du sous-domaine appartiennent au dept).
  (Le slug dept dans le CHEMIN sur le domaine www, lui, est IGNORÉ serveur → ne pas l'utiliser.)

Pagination : la page 1 du sous-domaine expose le vrai pattern paginé dans le
  lien « Suivant » : /annonces/{region-slug}-{dept-slug}/vente/maison/{N}
  (ex: /annonces/centre-val-de-loire-loiret/vente/maison/2). On lit ce préfixe
  région-dept dynamiquement sur la page 1 (robuste : pas de table région en dur).
  15 cartes/page. Les depts à ≤15 annonces n'ont qu'une page (pas de lien next).

Cartes : div.row.product
  - URL/Titre : h3 a[href]   → /annonce-{dept}-{ville}/{id}-{slug}
  - Loc       : p.product-location  →  "Fleury-les-Aubrais 45400"  (ville + CP complet)
  - Prix      : p.product-price      →  "379 000 €"
  - Specs     : <p> avec span.fw-bold  →  "Maison • 148 m² • 5 pièces • 4 chambres • 522 m² terrain"
  - Type      : h4.h5  →  "Ventes immobilières Maison"  (+ 1er span des specs)
  - Photo     : div.thumbnail img[data-src]

Particularités : annonces de particuliers, DPE rarement renseigné (→ dpe=None,
  comme le_tuc). Description non exposée sur la carte (titre uniquement).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int
from scrapers._base import parse_price_digits as _parse_price

BASE_TPL = "https://{slug}.immo-entre-particuliers.com"
LISTING_PATH = "/annonces/vente/maison"
MAX_PAGES = 12
PHOTOS_PER_CARD = 10


# Code département → slug de sous-domaine immo-entre-particuliers.com
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

# Types de bien à conserver (1er span des specs / segment de type)
_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|long[eè]re|manoir|chateau|château|"
    r"moulin|demeure|domaine|mas|g[iî]te|corps.de.ferme|maison.de.village",
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
                print(f"[ImmoEntreParticuliers] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[ImmoEntreParticuliers] Erreur dept {dept}: {e}")
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
    base = BASE_TPL.format(slug=slug)
    biens: list[dict] = []
    seen_ids: set[str] = set()
    page_tpl: str | None = None  # "/annonces/{region-dept}/vente/maison/{N}"

    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = base + LISTING_PATH
        elif page_tpl:
            url = base + page_tpl.format(n=page)
        else:
            break  # une seule page (pas de pagination découverte)

        r = await client.get(url)
        if r.status_code != 200:
            break

        soup = BeautifulSoup(r.text, "html.parser")

        if page == 1:
            page_tpl = _discover_page_template(soup)

        cards = soup.select("div.row.product")
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            try:
                bien = _parse_card(card, dept, base)
            except Exception:
                continue
            if not bien:
                continue

            # Sécurité : on n'accepte STRICTEMENT que le département cible
            if not bien["code_postal"] or bien["code_postal"][:2] != dept:
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


def _discover_page_template(soup) -> str | None:
    """Lit le vrai pattern paginé dans le lien 'Suivant' de la page 1.

    href: /annonces/{region-slug}-{dept-slug}/vente/maison/2
       →  "/annonces/{region-dept}/vente/maison/{n}"
    """
    nxt = soup.select_one("ul.pagination a[rel=next]")
    if not nxt:
        return None
    href = nxt.get("href", "")
    m = re.search(r"^(/annonces/[a-z-]+/vente/maison)/\d+$", href)
    if not m:
        return None
    return m.group(1) + "/{n}"


def _parse_card(card, dept: str, base: str) -> dict | None:
    link = card.select_one("h3 a[href]") or card.select_one("a.box-link[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else base + href

    # id_annonce : segment numérique du slug /annonce-.../{id}-...
    id_annonce = ""
    m_id = re.search(r"/(\d+)-", href)
    if m_id:
        id_annonce = m_id.group(1)
    if not id_annonce:
        id_annonce = url

    # Localisation : "Ville 45400"
    loc_el = card.select_one("p.product-location")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)

    # Titre
    title_el = card.select_one("h3 a")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    # Type de bien : h4 ("Ventes immobilières Maison") + 1er span des specs
    type_el = card.select_one("h4")
    type_txt = type_el.get_text(" ", strip=True) if type_el else ""
    specs_p = _specs_paragraph(card)
    first_span = specs_p.select_one("span.fw-bold") if specs_p else None
    type_hint = first_span.get_text(strip=True) if first_span else ""
    type_blob = f"{type_txt} {type_hint}"
    if _EXCLUDE_TYPE.search(type_blob) and not _KEEP_TYPE.search(type_blob):
        return None
    if not _KEEP_TYPE.search(type_blob):
        return None
    m_type = _KEEP_TYPE.search(type_blob)
    type_bien = (m_type.group(0).lower() if m_type else "maison")

    # Prix
    price_el = card.select_one("p.product-price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Specs : surface / pièces / chambres / terrain
    specs_txt = specs_p.get_text(" ", strip=True) if specs_p else ""
    surface = _parse_measure(r"(\d[\d\s\xa0]*)\s*m²(?!\s*terrain)", specs_txt, kind="surf")
    surface_terrain = _parse_measure(
        r"(\d[\d\s\xa0]*)\s*m²\s*terrain", specs_txt, kind="terr"
    )
    pieces = _parse_int(r"(\d+)\s*pi[eè]ces?", specs_txt)
    chambres = _parse_int(r"(\d+)\s*chambres?", specs_txt)

    # Photo
    photos = []
    for img in card.select("img"):
        src = img.get("data-src") or img.get("data-original") or ""
        if not src or src.startswith("data:") or "pixel" in src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "immo_entre_particuliers",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",  # non exposée sur la carte
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Immo entre Particuliers",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _specs_paragraph(card):
    """Le <p> des specs est celui qui contient 'm²' (≠ product-location/price)."""
    for p in card.select("p"):
        cls = p.get("class") or []
        if "product-location" in cls or "product-price" in cls:
            continue
        if "m²" in p.get_text():
            return p
    return None


def _parse_loc(text: str) -> tuple[str, str]:
    """'Fleury-les-Aubrais 45400' → ('Fleury-les-Aubrais', '45400')"""
    cp = ""
    m_cp = re.search(r"(\d{5})", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\d{5}\s*$", "", text).strip()
    return ville, cp


def _parse_measure(pattern: str, text: str, kind: str) -> float | None:
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
    except ValueError:
        return None
    if kind == "surf" and not (8 <= f <= 2000):
        return None
    if kind == "terr" and not (1 <= f <= 5_000_000):
        return None
    return f


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
    print(f"\nTotal Immo entre Particuliers: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['pieces'] or '?'}p/{b['chambres'] or '?'}ch"
            f" — {b['type_bien']} — {b['ville']}"
        )

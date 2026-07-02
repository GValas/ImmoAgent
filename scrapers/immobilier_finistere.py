"""scrapers/immobilier_finistere.py — Portail Immobilier Finistère (CMS Polaris)

Site : https://www.immobilier-finistere.com
       Agence / portail multi-office (Brest et grand Finistère).

Méthode : scrape_simple (httpx) — SSR HTML brut (Apache, status 200, pas de
          Cloudflare). Liste ET fiches sont dans le HTML sans JS.

URL pattern (catégories VENTE) :
    /type_bien/{cat}_{page}.html?cPath={cat_underscore}
      - 4-40  → Maisons à vendre  (cPath=4_40)
      - 8     → Immeubles         (cPath=8)
    9 cartes / page. La fiche détail :
    /fiches/{code}_{id}/{ville-slug}.html

Stratégie filtre département : AUCUN paramètre département fiable côté serveur.
    Le site est mono-département de fait (Finistère 29) MAIS contient quelques
    annexes hors-zone (ex. Montreuil 93, Paris 75). Le code postal N'EST PAS sur
    la carte de liste (seul le nom de ville y figure). → on récupère le CP
    AUTORITAIRE sur la fiche détail puis POST-FILTRE STRICT code_postal[:2]==dept.
    Pour limiter les requêtes, on n'ouvre la fiche que des biens passant déjà le
    pré-filtre prix/surface lu sur la carte.

Cartes liste : div.col-md-4 (contenant un a[href*='fiches/'])
    - URL    : a[href*='fiches/']
    - id     : data-productid (bouton favori) ou id numérique du slug
    - Titre  : h5  →  "BREST - Maison 103 m2 - 4 CH. 196 100 €"
    - Prix   : span.prix  →  "196 100 €"
    - Surface/chambres : extraits du titre ("103 m2", "4 CH.")
    - Ville  : début du titre (avant le 1er ' - ')
    - Photos : background: url(...) des div.item du carousel

Fiche détail (key/value) :
    <div class="col-sm-6">LABEL</div><div class="col-sm-6"><b>VALUE</b></div>
    → Code postal, Ville, Surface, Surface terrain, Nombre pièces, Chambres,
      Prix, Consommation énergie primaire (DPE).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.immobilier-finistere.com"
MAX_PAGES = 14
PHOTOS_PER_CARD = 10
DETAIL_CONCURRENCY = 6

# Catégories VENTE à scraper (maisons + immeubles). On exclut volontairement
# appartements/locations/terrains/professionnels.
SALE_CATEGORIES = [
    ("4-40", "4_40"),   # Maisons à vendre
    ("8", "8"),         # Immeubles
]


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1) Collecte des cartes (toutes catégories vente), pré-filtre prix/surface
        candidates: list[dict] = []
        seen_ids: set[str] = set()
        for cat, cpath in SALE_CATEGORIES:
            cat_cands = await _scrape_category(
                client, cat, cpath, prix_max, prix_min, surface_min, seen_ids
            )
            candidates.extend(cat_cands)
        print(
            f"[ImmoFinistere] {len(candidates)} cartes candidates "
            f"(après pré-filtre prix/surface)"
        )

        # 2) Enrichissement fiche (CP autoritaire) + post-filtre dept STRICT
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def enrich(card: dict) -> dict | None:
            async with sem:
                bien = await _build_from_detail(client, card)
                await asyncio.sleep(0.4)
            if not bien:
                return None
            cp = bien.get("code_postal") or ""
            # Filtre département STRICT : 0 fuite hors-zone
            if not cp or cp[:2] not in departements:
                return None
            bien["departement"] = cp[:2]
            # Re-vérifie les bornes avec les valeurs autoritaires de la fiche
            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                return None
            if prix_min and p and p < prix_min:
                return None
            if surface_min and s and s < surface_min:
                return None
            return bien

        biens = await asyncio.gather(*(enrich(c) for c in candidates))
        results = [b for b in biens if b]

    # Log par département
    from collections import Counter

    per_dept = Counter(b["departement"] for b in results)
    for d in sorted(per_dept):
        print(f"[ImmoFinistere] Dept {d}: {per_dept[d]} annonces")
    print(f"[ImmoFinistere] Total retenu : {len(results)}")
    return results


async def _scrape_category(
    client: httpx.AsyncClient,
    cat: str,
    cpath: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
    seen_ids: set[str],
) -> list[dict]:
    cands: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/type_bien/{cat}_{page}.html?cPath={cpath}"
        try:
            r = await client.get(url)
        except Exception as e:
            print(f"[ImmoFinistere] Erreur cat {cat} page {page}: {e}")
            break
        if r.status_code != 200:
            break

        soup = BeautifulSoup(r.text, "html.parser")
        cards = [
            c
            for c in soup.select("div.col-md-4")
            if c.find("a", href=re.compile(r"fiches/"))
        ]
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            parsed = _parse_card(card)
            if not parsed:
                continue
            aid = parsed["id_annonce"]
            if aid in seen_ids:
                continue

            # Pré-filtre prix/surface (valeurs de la carte) pour limiter les
            # ouvertures de fiche. On ne rejette pas si le champ est inconnu.
            p = parsed.get("prix") or 0
            s = parsed.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            seen_ids.add(aid)
            cands.append(parsed)
            new_on_page += 1

        if new_on_page == 0:
            break
        await asyncio.sleep(0.5)

    return cands


def _parse_card(card) -> dict | None:
    link = card.find("a", href=re.compile(r"fiches/"))
    if not link:
        return None
    href = link.get("href", "")
    url = _abs_url(href)

    # id annonce : data-productid sinon id numérique du slug d'URL
    id_annonce = ""
    fav = card.find(attrs={"data-productid": True})
    if fav:
        id_annonce = str(fav.get("data-productid")).strip()
    if not id_annonce:
        m = re.search(r"_(\d+)/", href)
        if m:
            id_annonce = m.group(1)
    if not id_annonce:
        id_annonce = url

    h5 = card.find("h5")
    titre = h5.get_text(" ", strip=True) if h5 else ""

    price_el = card.find("span", class_="prix")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else titre)

    # Ville = segment avant le 1er ' - ' du titre ("BREST - Maison 103 m2 ...")
    ville = ""
    if titre:
        ville = titre.split(" - ")[0].strip()

    surface = _parse_surface(titre)
    chambres = _parse_int(r"(\d+)\s*CH", titre)

    desc_el = card.find("p")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    photos = _parse_photos(card)

    return {
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "ville_carte": ville,
        "surface": surface,
        "chambres": chambres,
        "prix": prix,
        "description": description[:1200],
        "photos": photos,
    }


async def _build_from_detail(client: httpx.AsyncClient, card: dict) -> dict | None:
    """Ouvre la fiche, en extrait le CP autoritaire et complète les champs."""
    fields: dict[str, str] = {}
    try:
        r = await client.get(card["url"])
        if r.status_code == 200:
            fields = _parse_detail_fields(r.text)
    except Exception:
        fields = {}

    code_postal = _digits5(fields.get("Code postal", ""))
    ville = fields.get("Ville", "").strip() or card.get("ville_carte", "")

    surface = _parse_surface(fields.get("Surface", "")) or card.get("surface")
    surface_terrain = _parse_surface(fields.get("Surface terrain", ""))
    pieces = _parse_int(r"(\d+)", fields.get("Nombre pièces", ""))
    chambres = _parse_int(r"(\d+)", fields.get("Chambres", "")) or card.get(
        "chambres"
    )
    prix = _parse_price(fields.get("Prix", "")) or card.get("prix")
    dpe = _parse_dpe(fields.get("Consommation énergie primaire", ""))
    type_bien = fields.get("Type de bien", "").strip().lower() or "maison"

    return {
        "source": "immobilier_finistere",
        "url": card["url"],
        "id_annonce": card["id_annonce"],
        "titre": card["titre"] or f"{type_bien.title()} {ville}".strip(),
        "type_bien": type_bien,
        "description": card.get("description", ""),
        "departement": code_postal[:2] if code_postal else None,
        "ville": (ville or "")[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": card.get("photos", []),
        "dpe": dpe,
        "agence": "Immobilier Finistère",
    }


def _parse_detail_fields(html: str) -> dict[str, str]:
    """Extrait les paires <div class='col-sm-6'>LABEL</div>
    <div class='col-sm-6'><b>VALUE</b></div> de la fiche."""
    fields: dict[str, str] = {}
    for m in re.finditer(
        r'<div class="col-sm-6">\s*([^<]{2,40}?)\s*</div>\s*'
        r'<div class="col-sm-6">\s*<b>\s*([^<]{1,60}?)\s*</b>',
        html,
    ):
        label = m.group(1).strip()
        value = m.group(2).strip()
        if label and label not in fields:
            fields[label] = value
    return fields


# ── Helpers ──────────────────────────────────────────────────────────────────

def _abs_url(href: str) -> str:
    if href.startswith("http"):
        return href
    href = href.lstrip(".")  # enlève les '../'
    if not href.startswith("/"):
        href = "/" + href
    return BASE_URL + href


def _parse_photos(card) -> list[str]:
    photos: list[str] = []
    for item in card.select("div.item"):
        style = item.get("style", "")
        m = re.search(r"url\(([^)]+)\)", style)
        if not m:
            continue
        src = m.group(1).strip("'\" ")
        src = _abs_url(src) if not src.startswith("http") else src
        if src and not src.startswith("data:") and src not in photos:
            photos.append(src)
    return photos[:PHOTOS_PER_CARD]


def _digits5(text: str) -> str:
    m = re.search(r"\b(\d{5})\b", text or "")
    return m.group(1) if m else ""


def _parse_price(text: str) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d]", "", text.split("EUR")[0].split("€")[0])
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    if v and v < 1000:  # garde-fou (ex. "5 CH" mal capturé)
        return None
    return v


def _parse_surface(text: str) -> float | None:
    """'103 m2' / '232 m2' / 'Maison 103 m2' → 103.0"""
    if not text:
        return None
    m = re.search(r"([\d\s\xa0]+)\s*m[²2]", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
        return f if 1 <= f <= 100000 else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    if not text:
        return None
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_dpe(text: str) -> str | None:
    if not text:
        return None
    t = text.strip().upper()
    return t if t in {"A", "B", "C", "D", "E", "F", "G"} else None


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
    print(f"\nTotal Immobilier Finistère : {len(biens)} annonces")
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

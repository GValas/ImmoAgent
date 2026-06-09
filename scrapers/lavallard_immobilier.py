"""scrapers/lavallard_immobilier.py — Lavallard Immobilier (agence familiale Somme/Picardie)

Méthode : scrape_simple (httpx) — SSR « CSR-like » : la page /vente est rendue
côté client (React, classes CSS hashées), MAIS les données complètes des annonces
sont injectées dans le HTML sous forme d'un blob JSON encodé en base64
(`JSON.parse(b64_to_utf8("...."))`). On décode ce blob → pas besoin de Playwright.
Plateforme NettyImmo (img.netty.immo / webapi/).

URL pattern : /vente?page={N}   (10-11 annonces/page, prodCount total connu)
  → PAS de filtre département côté serveur fiable (l'agence ne couvre que la
    Picardie : 80 + quelques 02 et 62). On scrape le national et on POST-FILTRE
    STRICT sur cp[:2] == dept cible → 0 fuite.

Données par produit (dans le blob, dict avec 'prod_ref' + 'cp') :
  - prod_ref   : id_annonce (ex: VM4734)
  - cp / city  : code postal / ville
  - prod_type  : house | land | build | ...  (on garde house/maison + assimilés)
  - title.fr   : titre commercial (peut être None) ; sinon meta_title.fr
  - url.fr     : slug détail → https://www.lavallard-immobilier.com/{slug}
  - formated.price.amount : prix (€)
  - surface    : surface habitable (m²)  /  land : surface terrain (m²)
  - rooms / rooms2 : pièces / chambres
  - photos     : liste d'URLs
  - meta_desc.fr : description

Couverture : agence mono-zone Picardie (Somme 80 surtout). Sur les départements
cibles Val-de-Loire / Ouest (72, 28, 45, 89...) : 0 stock attendu (hors zone).
Scraper conservé et fonctionnel ; réactiver si la zone cible inclut 80/02/62.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import base64
import json
import re

import httpx

BASE_URL = "https://www.lavallard-immobilier.com"
MAX_PAGES = 20
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_B64_RE = re.compile(r'JSON\.parse\(b64_to_utf8\("([A-Za-z0-9+/=]+)"\)\)')

# prod_type NettyImmo → on conserve les maisons / biens « habitables » assimilés.
_KEEP_TYPES = {"house", "villa", "property", "farm", "mansion", "castle"}
# Exclus explicites (terrains, immeubles de rapport, locaux pro, parkings...)
_EXCLUDE_TYPES = {
    "land", "build", "shop", "office", "parking", "garage", "business",
    "premise", "warehouse", "land_const",
}


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    results: list[dict] = []
    seen_refs: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            try:
                prods = await _fetch_page(client, page)
            except Exception as e:
                print(f"[Lavallard] Erreur page {page}: {e}")
                break

            if not prods:
                break

            new_on_page = 0
            for prod in prods.values():
                ref = prod.get("prod_ref")
                if not ref or ref in seen_refs:
                    continue
                seen_refs.add(ref)
                new_on_page += 1

                bien = _parse_prod(prod)
                if not bien:
                    continue

                cp = bien["code_postal"] or ""
                # POST-FILTRE DÉPARTEMENT STRICT → 0 fuite hors-zone
                if not cp or cp[:2] not in departements:
                    continue
                bien["departement"] = cp[:2]

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                results.append(bien)

            # plus aucun nouveau ref → fin de pagination
            if new_on_page == 0:
                break
            await asyncio.sleep(0.6)

    print(f"[Lavallard] {len(results)} annonces (depts {departements})")
    return results


async def _fetch_page(client: httpx.AsyncClient, page: int) -> dict:
    """Récupère la page /vente?page=N et renvoie {prod_ref: prod_dict}."""
    url = f"{BASE_URL}/vente?page={page}"
    r = await client.get(url)
    if r.status_code != 200:
        return {}
    return _extract_products(r.text)


def _extract_products(html: str) -> dict:
    """Décode les blobs base64 du HTML et collecte les objets-produits."""
    products: dict = {}
    for m in _B64_RE.finditer(html):
        try:
            raw = base64.b64decode(m.group(1)).decode("utf-8")
        except Exception:
            continue
        if "prod_ref" not in raw or "prodResults" not in raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        _collect(data, products)
        if products:
            break
    return products


def _collect(node, out: dict) -> None:
    """Parcourt récursivement le JSON et capture les dicts produits (prod_ref+cp)."""
    if isinstance(node, dict):
        if "prod_ref" in node and "cp" in node:
            ref = node.get("prod_ref")
            if ref and ref not in out:
                out[ref] = node
        for v in node.values():
            _collect(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect(v, out)


def _parse_prod(prod: dict) -> dict | None:
    ref = prod.get("prod_ref")
    if not ref:
        return None

    prod_type = (prod.get("prod_type") or "").lower()
    if prod_type in _EXCLUDE_TYPES:
        return None
    if _KEEP_TYPES and prod_type and prod_type not in _KEEP_TYPES:
        return None

    cp = str(prod.get("cp") or "").strip()
    ville = (prod.get("city") or "").strip()

    slug = _lang(prod.get("url"))
    url = f"{BASE_URL}/{slug}" if slug else BASE_URL

    titre = _lang(prod.get("title")) or _lang(prod.get("meta_title")) or ""
    if not titre:
        titre = f"Maison {ville}".strip()

    description = _lang(prod.get("meta_desc")) or ""

    prix = _price(prod)
    surface = _num(prod.get("surface"))
    surface_terrain = _num(prod.get("land"))
    pieces = _num(prod.get("rooms"))
    chambres = _num(prod.get("rooms2"))

    photos = []
    raw_photos = prod.get("photos") or []
    if isinstance(raw_photos, list):
        for ph in raw_photos:
            if isinstance(ph, str) and ph.startswith("http"):
                photos.append(ph)
    if not photos and isinstance(prod.get("image"), str):
        photos.append(prod["image"])
    photos = photos[:PHOTOS_PER_CARD]

    type_bien = "maison" if prod_type in {"house", "villa", "property"} else (
        prod_type or "maison"
    )

    return {
        "source": "lavallard_immobilier",
        "url": url,
        "id_annonce": ref,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": cp[:2] if cp else None,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Lavallard Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _lang(val) -> str:
    """Champ multilingue NettyImmo {'fr': '...'} → str FR."""
    if isinstance(val, dict):
        return (val.get("fr") or next(iter(val.values()), "") or "").strip()
    if isinstance(val, str):
        return val.strip()
    return ""


def _num(val) -> float | None:
    """Convertit un champ numérique éventuellement string en float (>0)."""
    if val is None:
        return None
    try:
        f = float(str(val).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None
    return f if f > 0 else None


def _price(prod: dict) -> float | None:
    """Prix depuis formated.price.amount (sinon price2/price1)."""
    fmt = prod.get("formated")
    if isinstance(fmt, dict):
        pr = fmt.get("price")
        if isinstance(pr, dict):
            a = pr.get("amount")
            if a:
                return _num(a)
    for k in ("price2", "price1", "fa"):
        v = _num(prod.get(k))
        if v:
            return v
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
    print(f"\nTotal Lavallard Immobilier: {len(biens)} annonces")
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

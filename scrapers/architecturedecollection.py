"""scrapers/architecturedecollection.py — Architecture de Collection

Maisons d'architecte / de caractère (agence parisienne, inventaire national restreint).

Méthode : scrape_simple (httpx) — site WooCommerce SSR.
Listing  : /categorie-produit/a-vendre/ (+ /page/N/), ~50 cartes/page, 2 pages.
           Chaque carte = lien vers une fiche /produit/{slug}/.
Fiche    : div.summary contient un bloc structuré pipe-séparé :
           "Villa moderne | 1929-2020 | Montargis | (45) | 590 000 € | 350 m² |
            5/7 chambres | 2 salles de bain | Jardin : 1 200 m² | Garage | ..."
           Le code département est entre parenthèses : "(45)".
Photos   : .woocommerce-product-gallery img (data-large_image / data-nectar-img-src).

Filtre département : l'inventaire est NATIONAL mais petit (~80 biens uniques) →
POST-FILTRE par le code département "(NN)" lu sur chaque fiche (voie b, comme remax/era).
La localisation "Paris 16e", l'étranger, etc. ne portent pas de "(NN)" cible et sont
donc correctement exclus.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.architecturedecollection.fr"
LISTING_URL = f"{BASE_URL}/categorie-produit/a-vendre/"
MAX_LISTING_PAGES = 5          # garde-fou ; en pratique 2 pages existent
MAX_PRODUCTS = 200             # garde-fou
PHOTOS_PER_BIEN = 10
CONCURRENCY = 8


# Mots-clés indiquant un appartement (à exclure — on veut maisons/propriétés)
_APPART_RE = re.compile(
    r"appartement|duplex|triplex|studio|loft|atelier d'artiste",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        product_urls = await _collect_product_urls(client)
        if not product_urls:
            print("[ArchiCollection] Aucune fiche trouvée sur le listing")
            return []

        sem = asyncio.Semaphore(CONCURRENCY)

        async def fetch(u: str):
            async with sem:
                try:
                    r = await client.get(u)
                    if r.status_code != 200:
                        return None
                    return _parse_product(r.text, u)
                except Exception:
                    return None

        parsed = await asyncio.gather(*[fetch(u) for u in product_urls])

    results: list[dict] = []
    seen: set[str] = set()
    for bien in parsed:
        if not bien:
            continue
        dept = bien.get("departement")
        if departements and dept not in departements:
            continue
        prix = bien.get("prix") or 0
        surf = bien.get("surface") or 0
        if prix_max and prix and prix > prix_max:
            continue
        if prix_min and prix and prix < prix_min:
            continue
        if surface_min and surf and surf < surface_min:
            continue
        aid = bien.get("id_annonce") or bien.get("url")
        if aid in seen:
            continue
        seen.add(aid)
        results.append(bien)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[ArchiCollection] Dept {dept}: {n} annonces")

    return results


async def _collect_product_urls(client: httpx.AsyncClient) -> list[str]:
    urls: list[str] = []
    for page in range(1, MAX_LISTING_PAGES + 1):
        url = LISTING_URL if page == 1 else f"{LISTING_URL}page/{page}/"
        try:
            r = await client.get(url)
        except Exception:
            break
        if r.status_code != 200:
            break
        found = re.findall(
            r'href="(' + re.escape(BASE_URL) + r'/produit/[^"#?]+)"', r.text
        )
        new = [u for u in found if u not in urls]
        urls.extend(new)
        if not new:
            break
        await asyncio.sleep(0.4)
    return urls[:MAX_PRODUCTS]


def _parse_product(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")

    summary = soup.select_one("div.summary")
    fields: list[str] = []
    if summary:
        raw = summary.get_text("|", strip=True)
        # Coupe la partie commerciale (CTA / catégories) après "Demander une visite"
        raw = re.split(r"Demander une visite|Catégories", raw)[0]
        fields = [f.strip() for f in raw.split("|") if f.strip()]

    # og:description en repli pour la localisation/prix si la fiche n'a pas de summary
    og = soup.find("meta", property="og:description")
    og_desc = og["content"].strip() if og and og.get("content") else ""

    summary_text = " ".join(fields)
    source_text = summary_text if summary_text else og_desc

    # ── Département : code "(NN)" ou "(2A)/(2B)" ────────────────────────────
    dept = None
    for m in re.finditer(r"\((\d{2,3}|2[AB])\)", source_text):
        code = m.group(1)
        dept = code[:2]
        break
    if not dept:
        return None  # pas de localisation département exploitable (Paris arrt., étranger…)

    # ── Titre ────────────────────────────────────────────────────────────
    h1 = soup.find("h1")
    titre = (h1.get_text(strip=True) if h1 else "") or (fields[0] if fields else "")
    titre = titre[:150]

    # ── Type de bien ─────────────────────────────────────────────────────
    type_bien = "appartement" if _APPART_RE.search(titre) else "maison"

    # ── Ville : champ texte avant le "(NN)" ──────────────────────────────
    ville = _extract_ville(fields, source_text)

    # ── Prix ─────────────────────────────────────────────────────────────
    prix = _parse_price(source_text)

    # ── Surface habitable : premier "NNN m²" ─────────────────────────────
    surface = _parse_surface(source_text)

    # ── Surface terrain : "Terrain : N m²" / "N hectares" / "Jardin : N m²"
    surface_terrain = _parse_terrain(source_text)

    # ── Chambres : "5 chambres" ou "5/7 chambres" ────────────────────────
    chambres = _parse_chambres(source_text)

    # ── id_annonce : post-id WooCommerce ─────────────────────────────────
    id_annonce = None
    body = soup.find("body")
    if body:
        for c in body.get("class", []):
            m = re.match(r"postid-(\d+)", c)
            if m:
                id_annonce = m.group(1)
                break
    if not id_annonce:
        id_annonce = url.rstrip("/").rsplit("/", 1)[-1]

    # ── Photos ───────────────────────────────────────────────────────────
    photos = _extract_photos(soup)

    return {
        "source": "architecturedecollection",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre,
        "type_bien": type_bien,
        "description": source_text[:1200],
        "departement": dept,
        "ville": ville,
        "code_postal": None,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Architecture de Collection",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────


def _extract_ville(fields: list[str], text: str) -> str | None:
    # Dans le bloc pipe-séparé, la ville est le champ juste avant "(NN)"
    for i, f in enumerate(fields):
        if re.fullmatch(r"\(\d{2,3}\)|\(2[AB]\)", f) and i > 0:
            v = fields[i - 1]
            v = re.sub(r"^(Proche|À proximité de|A proximité de)\s+", "", v, flags=re.I)
            return v[:80] or None
    # Repli : capture "Ville (NN)" dans le texte
    m = re.search(r"([A-ZÀ-Ÿ][\wÀ-ÿ'’\- ]{1,60}?)\s*\((?:\d{2,3}|2[AB])\)", text)
    if m:
        v = re.sub(r"^(Proche|À proximité de|A proximité de)\s+", "", m.group(1).strip(), flags=re.I)
        return v[:80] or None
    return None


def _parse_price(text: str) -> float | None:
    # "590 000 €" — évite de capter un prix au m². Exclut "Prix sur demande".
    for m in re.finditer(r"([\d][\d    ]{2,})\s*€", text):
        cleaned = re.sub(r"[\s  ]", "", m.group(1))
        try:
            val = float(cleaned)
        except ValueError:
            continue
        if val >= 1000:  # ignore les "€/m²" ou montants parasites
            return val
    return None


def _parse_surface(text: str) -> float | None:
    # Premier "NNN m²" (surface habitable). Gère décimales "149,81 m²".
    m = re.search(r"([\d][\d    ]*(?:[.,]\d+)?)\s*m²", text)
    if m:
        val = re.sub(r"[\s  ]", "", m.group(1)).replace(",", ".")
        try:
            return float(val)
        except ValueError:
            return None
    return None


def _parse_terrain(text: str) -> float | None:
    # Hectares
    m = re.search(r"([\d]+(?:[.,]\d+)?)\s*hectares?", text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(",", ".")) * 10000
        except ValueError:
            pass
    # "Terrain : 667 m²" ou "Jardin : 1 200 m²"
    m = re.search(
        r"(?:Terrain|Jardin|Parc)[^:]*:\s*([\d][\d    ]*)\s*m²",
        text,
        re.IGNORECASE,
    )
    if m:
        val = re.sub(r"[\s  ]", "", m.group(1))
        try:
            return float(val)
        except ValueError:
            pass
    return None


def _parse_chambres(text: str) -> int | None:
    # "5/7 chambres" → 5 ; "5 chambres" → 5
    m = re.search(r"(\d+)\s*(?:/\s*\d+\s*)?chambres?", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _extract_photos(soup: BeautifulSoup) -> list[str]:
    photos: list[str] = []
    gallery = soup.select(
        ".woocommerce-product-gallery img, div.images img, figure.woocommerce-product-gallery__image img"
    )
    for img in gallery:
        src = (
            img.get("data-large_image")
            or img.get("data-nectar-img-src")
            or img.get("src")
        )
        if not src:
            continue
        src = src.split("?")[0]
        if "/wp-content/uploads/" not in src:
            continue
        if any(x in src.lower() for x in ("favicon", "-menu", "placeholder", "logo")):
            continue
        if src.startswith("data:"):
            continue
        if src not in photos:
            photos.append(src)
        if len(photos) >= PHOTOS_PER_BIEN:
            break
    return photos


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
    print(f"\nTotal Architecture de Collection: {len(biens)} annonces")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:60]}"
            f" — {b['prix']}€"
            f" — {b.get('surface', '?')}m²"
            f" — {b.get('chambres', '?')}ch"
            f" — {b['ville']}"
            f" — {len(b['photos'])} photos"
        )

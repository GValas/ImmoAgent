"""scrapers/maryimmobilier.py — Mary Immobilier (Saint-Amand-en-Puisaye, Nièvre)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress, thème wpcasa/casanova)
Site : http://maryimmobilier.fr — agence indépendante de Saint-Amand-en-Puisaye,
secteur Puisaye (Nièvre 58, en limite Yonne 89 / Loiret 45 / Cher 18).

URL : /property/  — archive SSR de tous les biens (~12 cartes, pas de pagination
exploitable). Aucun filtre département serveur.

Cartes : div.property
  - Titre   : .post-title           → "Réf 2347 SAINT AMAND EN PUISAYE"
  - Localité: classe CSS location-{slug} (ex. location-st-amand-en-puisaye)
  - Type    : classe CSS property-type-{type} (ex. property-type-pavillon)
  - Pièces  : classe property-category-{N}-pieces
  - Détails : .listing-details-1 (chambres), .listing-details-3 (terrain m²)
  - Prix    : .listing-price-value  → "85.000"
  - Teaser  : .post-teaser          → "Pavillon 68 m² sur sous sol" (→ surface)

FILTRE DÉPARTEMENT (0 fuite) : aucun code postal dans la carte. On extrait la
commune (classe location-{slug}, st→saint normalisé) et on la RÉSOUT en
(département, code_postal) via scrapers._geo_resolver.resolve_communes, restreint
aux départements cibles → une commune hors-zone renvoie (None, None) et le bien
est écarté. Post-filtre strict sur le département résolu.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_price, parse_surface
from scrapers._geo_resolver import resolve_communes

BASE_URL = "http://maryimmobilier.fr"
LIST_URL = f"{BASE_URL}/property/"
PHOTOS_PER_CARD = 1

_EXCLUDE_TYPE = re.compile(
    r"terrain|garage|parking|local|commerce|immeuble|bureau|fonds",
    re.IGNORECASE,
)


def _location_from_classes(card) -> str:
    """Extrait le nom de commune depuis la classe CSS location-{slug}."""
    for c in card.get("class", []):
        if c.startswith("location-"):
            slug = c[len("location-"):].replace("-", " ").strip()
            # wpcasa abrège "Saint" en "st"
            slug = re.sub(r"^st\b", "saint", slug)
            slug = re.sub(r"\bst\b", "saint", slug)
            return slug
    # repli : nom en majuscules dans le titre
    t = card.select_one(".post-title")
    if t:
        m = re.search(r"\b([A-ZÀ-Ý][A-ZÀ-Ý '\-]{3,})$", t.get_text(" ", strip=True))
        if m:
            return m.group(1).title()
    return ""


def _type_from_classes(card) -> str:
    for c in card.get("class", []):
        if c.startswith("property-type-"):
            return c[len("property-type-"):].replace("-", " ").strip()
    return "maison"


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_min = criteres.get("prix_min", 0)
    prix_max = criteres.get("prix_max", 0)
    surface_min = criteres.get("surface_min", 0)

    async with make_client() as client:
        r = await get_with_retry(client, LIST_URL)
        if r is None or r.status_code != 200:
            print(f"[MaryImmo] liste inaccessible (status {getattr(r, 'status_code', '?')})")
            return []
        cards = BeautifulSoup(r.text, "html.parser").select("div.property")

    # 1er passage : parse brut + collecte des communes à résoudre
    parsed: list[dict] = []
    for card in cards:
        try:
            bien = _parse_card(card)
        except Exception:
            continue
        if bien:
            parsed.append(bien)

    communes = [b["_commune"] for b in parsed if b.get("_commune")]
    mapping = await resolve_communes(communes, departements)

    # 2e passage : filtre département strict via la résolution
    results: list[dict] = []
    seen: set[str] = set()
    for b in parsed:
        from scrapers._geo_resolver import _norm
        key = _norm(b.get("_commune") or "")
        dept, cp = mapping.get(key, (None, None))
        if not dept or dept not in departements:
            continue
        b["departement"] = dept
        b["code_postal"] = cp or ""
        b.pop("_commune", None)
        aid = b.get("id_annonce") or b.get("url")
        if aid in seen:
            continue
        p = b.get("prix") or 0
        s = b.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue
        seen.add(aid)
        results.append(b)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print(f"[MaryImmo] {len(results)} annonces — {by_dept}")
    await asyncio.sleep(0)
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one('a[href*="/property/"]')
    if not link:
        return None
    href = link.get("href", "")
    url = href if href.startswith("http") else BASE_URL + href

    type_bien = _type_from_classes(card)
    if _EXCLUDE_TYPE.search(type_bien):
        return None

    commune = _location_from_classes(card)
    ville = commune.title() if commune else ""

    title_el = card.select_one(".post-title")
    titre_raw = title_el.get_text(" ", strip=True) if title_el else ""
    m_ref = re.search(r"R[ée]f\s*(\S+)", titre_raw)
    ref = m_ref.group(1) if m_ref else None

    teaser_el = card.select_one(".post-teaser")
    description = teaser_el.get_text(" ", strip=True) if teaser_el else ""

    price_el = card.select_one(".listing-price-value")
    prix = None
    if price_el:
        # "85.000" (point = séparateur de milliers)
        prix = parse_price(price_el.get_text(strip=True).replace(".", ""))

    # pièces depuis property-category-{N}-pieces
    pieces = None
    for c in card.get("class", []):
        m = re.match(r"property-category-(\d+)-pieces", c)
        if m:
            pieces = int(m.group(1))
            break

    chambres = None
    cb = card.select_one(".listing-details-1")
    if cb and cb.get_text(strip=True).isdigit():
        chambres = int(cb.get_text(strip=True))

    surface_terrain = None
    td = card.select_one(".listing-details-3")
    if td:
        m = re.search(r"([\d\s]+)\s*m", td.get_text())
        if m:
            try:
                surface_terrain = float(re.sub(r"\s", "", m.group(1)))
            except ValueError:
                pass

    surface = parse_surface(description) or parse_surface(titre_raw)
    if surface is None:
        m = re.search(r"(\d{2,4})\s*m[²2]", description)
        if m and 8 <= float(m.group(1)) <= 2000:
            surface = float(m.group(1))

    titre = titre_raw
    if commune and commune.lower() not in titre.lower():
        titre = f"{type_bien.title()} {ville}".strip()

    photos = []
    img = card.select_one("img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(src)

    return {
        "source": "maryimmobilier",
        "url": url,
        "id_annonce": ref or url,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": "",      # résolu dans search()
        "ville": ville[:80],
        "code_postal": "",      # résolu dans search()
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Mary Immobilier",
        "_commune": commune,
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Mary Immobilier")

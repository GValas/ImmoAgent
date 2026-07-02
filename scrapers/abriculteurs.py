"""scrapers/abriculteurs.py — Abriculteurs (néo-agence à frais fixes)

Méthode : scrape_simple (httpx) — SSR HTML + ld+json sur la page détail.

Couverture : nationale (~352 biens), majorité grandes métropoles, mais
quelques biens dans les départements cibles (ex : Tours / 37).

Filtre département : AUCUN filtre serveur par département/URL n'existe.
  → on scrape le listing national /fr/immobilier/vente?page=N (24/page, 15 pages),
    puis on POST-FILTRE strictement sur le code postal récupéré sur la page détail.
    Le code postal n'est PAS dans le listing (ville seule) : il faut donc visiter
    la page détail de chaque carte, où un bloc ld+json (PostalAddress) le fournit.
    Pour limiter la charge, les détails sont récupérés en parallèle (sémaphore).

Listing — cartes : div.item-property
  - URL    : a.property-img[href]  → /fr/immobilier/vente/{type}/{ville}/{slug}/{id}
  - Type   : 1ʳᵉ .critere (Appartement / Maison / Terrain / Commerce …)
  - Critères .critere : "N pièces", "NN m²", "N chambre(s)"
  - Ville  : .property-city
  - Prix   : .price  → "221 000 €"
  - Photos : picture source[srcset] / img[src] (cloudfront/s3 .webp)

Détail — ld+json {"@type":["Product", <SousType>]} :
  - address.postalCode  → filtre département FIABLE
  - address.addressLocality → ville
  - offers.price        → prix
  - numberOfRooms       → pièces
  - floorSize.value     → surface habitable
  - description         → texte complet

Type de bien : on ne garde que maisons / propriétés / appartements
  (exclut terrain, commerce, bureau, local, parking…).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.abriculteurs.com"
LISTING_PATH = "/fr/immobilier/vente"
MAX_PAGES = 16          # garde-fou ; le listing en a ~15
DETAIL_CONCURRENCY = 8
PHOTOS_PER_CARD = 10


# Types (segment d'URL / 1ʳᵉ critère) conservés
_KEEP_TYPE = re.compile(
    r"maison|appartement|propriete|propriété|villa|ferme|longere|longère|"
    r"manoir|chateau|château|moulin|demeure|domaine|mas|loft|duplex",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"terrain|commerce|bureau|local|garage|parking|immeuble|fonds|hangar",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1) Collecte de toutes les cartes du listing national
        cards = await _collect_cards(client)
        print(f"[Abriculteurs] {len(cards)} cartes collectées sur le listing national")

        # 2) Récupération des détails (CP) en parallèle, puis post-filtre dept
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def enrich(card):
            async with sem:
                return await _enrich_detail(client, card)

        enriched = await asyncio.gather(*(enrich(c) for c in cards))

    results: list[dict] = []
    seen: set[str] = set()
    for bien in enriched:
        if not bien:
            continue
        cp = bien.get("code_postal") or ""
        dept = cp[:2] if cp else ""
        # POST-FILTRE DÉPARTEMENT STRICT (0 fuite)
        if dept not in departements:
            continue
        bien["departement"] = dept

        aid = bien["id_annonce"]
        if aid in seen:
            continue

        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        seen.add(aid)
        results.append(bien)

    # Récap par département
    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print(f"[Abriculteurs] {len(results)} biens retenus dans la zone : {by_dept}")
    return results


async def _collect_cards(client: httpx.AsyncClient) -> list[dict]:
    cards: list[dict] = []
    seen_urls: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}{LISTING_PATH}?page={page}"
        try:
            r = await client.get(url)
        except Exception as e:
            print(f"[Abriculteurs] Erreur page {page}: {e}")
            break
        if r.status_code != 200:
            break
        items = BeautifulSoup(r.text, "html.parser").select("div.item-property")
        if not items:
            break
        new_on_page = 0
        for item in items:
            card = _parse_card(item)
            if not card or card["url"] in seen_urls:
                continue
            seen_urls.add(card["url"])
            cards.append(card)
            new_on_page += 1
        if new_on_page == 0:
            break
        await asyncio.sleep(0.5)
    return cards


def _parse_card(item) -> dict | None:
    link = item.select_one("a.property-img")
    href = link.get("href", "") if link else ""
    if not href or "/fr/immobilier/vente/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    parts = [p for p in href.split("/") if p]
    # /fr/immobilier/vente/{type}/{ville}/{slug}/{id}
    type_seg = parts[3] if len(parts) > 3 else ""
    id_annonce = parts[-1] if parts and parts[-1].isdigit() else url

    criteres_els = item.select(".criterias .critere")
    crit_txts = [c.get_text(" ", strip=True) for c in criteres_els]
    type_label = crit_txts[0] if crit_txts else type_seg
    type_bien = (type_label or type_seg).replace("-", " ").strip().lower() or "bien"

    # Filtre type (sur le label de carte ou le segment d'URL)
    blob = f"{type_seg} {type_label}"
    if _EXCLUDE_TYPE.search(blob) and not _KEEP_TYPE.search(blob):
        return None
    if not _KEEP_TYPE.search(blob):
        return None

    pieces = chambres = None
    surface = None
    for txt in crit_txts[1:]:
        if pieces is None and re.search(r"pi[èe]ce", txt, re.IGNORECASE):
            m = re.search(r"(\d+)", txt)
            pieces = int(m.group(1)) if m else None
        elif chambres is None and re.search(r"chambre", txt, re.IGNORECASE):
            m = re.search(r"(\d+)", txt)
            chambres = int(m.group(1)) if m else None
        elif surface is None and re.search(r"m²", txt):
            m = re.search(r"([\d\s\xa0,\.]+)\s*m²", txt)
            if m:
                surface = _to_float(m.group(1))

    city_el = item.select_one(".property-city")
    ville = city_el.get_text(" ", strip=True) if city_el else ""

    price_el = item.select_one(".price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    title_seg = parts[-2] if len(parts) >= 2 else ""
    titre = title_seg.replace("-", " ").strip().capitalize() or f"{type_bien} {ville}"

    photos = _extract_photos(item)

    return {
        "source": "abriculteurs",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": "",
        "ville": ville[:80],
        "code_postal": "",          # rempli depuis le détail
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Abriculteurs",
    }


async def _enrich_detail(client: httpx.AsyncClient, card: dict) -> dict | None:
    """Visite la page détail pour récupérer le code postal (ld+json) + champs riches."""
    try:
        r = await client.get(card["url"])
    except Exception:
        return card  # carte sans CP → sera filtrée hors-zone
    if r.status_code != 200:
        return card

    data = _extract_product_ldjson(r.text)
    if data:
        addr = data.get("address") or {}
        cp = str(addr.get("postalCode") or "").strip()
        if re.fullmatch(r"\d{5}", cp):
            card["code_postal"] = cp
        loc = addr.get("addressLocality")
        if loc:
            card["ville"] = str(loc)[:80]

        offers = data.get("offers") or {}
        price = offers.get("price")
        if price and not card.get("prix"):
            card["prix"] = _to_float(str(price))

        nrooms = data.get("numberOfRooms")
        if nrooms and not card.get("pieces"):
            try:
                card["pieces"] = int(nrooms)
            except (ValueError, TypeError):
                pass

        fs = data.get("floorSize") or {}
        val = fs.get("value")
        if val and not card.get("surface"):
            card["surface"] = _to_float(str(val))

        desc = data.get("description")
        if desc:
            card["description"] = str(desc)[:1200]

    # Repli : CP dans le HTML brut si ld+json muet
    if not card.get("code_postal"):
        m = re.search(r'"postalCode"\s*:\s*"(\d{5})"', r.text)
        if m:
            card["code_postal"] = m.group(1)

    await asyncio.sleep(0.05)
    return card


def _extract_product_ldjson(html: str) -> dict | None:
    for m in re.finditer(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S
    ):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        t = data.get("@type")
        if isinstance(t, list) and "Product" in t:
            return data
        if t == "Product" and "address" in data:
            return data
    return None


def _extract_photos(item) -> list[str]:
    photos: list[str] = []
    for src in item.select("picture source[srcset]"):
        url = src.get("srcset", "").split()[0] if src.get("srcset") else ""
        if url and not url.startswith("data:") and url not in photos:
            photos.append(url)
    if not photos:
        for img in item.select("img"):
            url = img.get("src") or ""
            if url and not url.startswith("data:") and url not in photos:
                photos.append(url)
    return photos[:PHOTOS_PER_CARD]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d.]", "", cleaned.replace(",", "."))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _to_float(text: str) -> float | None:
    cleaned = re.sub(r"[\s\xa0]", "", text).replace(",", ".")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
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
    print(f"\nTotal Abriculteurs: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

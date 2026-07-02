"""scrapers/proprietesdecharme.py — Propriétés De Charme (demeures de prestige nationales)

Méthode : scrape_simple (httpx) — SSR WordPress / thème Houzez.

ARCHITECTURE DU SITE
--------------------
- Pas de filtre département côté serveur. La seule taxonomie de localisation est
  `label/etiq_fr_{ville-slug}` (étiquettes par nom de ville, pas par département) et
  il n'existe aucune taxonomie `property-city` / `property-state` exploitable.
- Les CARTES de listing (`div.item-listing-wrap`) portent le titre, le prix et les
  équipements, mais le champ `item-address` est VIDE (adresse masquée en liste).
  → le code postal / département n'est disponible QUE sur la page détail.
- Chaque page détail expose un bloc JSON-LD `RealEstateListing` complet :
  `offers.price`, `address.postalCode`, `address.addressLocality`, `floorSize.value`.

Stratégie : on parcourt l'archive `/property-type/_maison/` (maisons/villas, ~277
pages, 9/page), on récupère les URLs détail, puis on télécharge les fiches pour en
extraire le code postal (JSON-LD) et on POST-FILTRE par `code_postal[:2]`.

LIMITE MAJEURE (→ actif: false)
-------------------------------
L'inventaire (~3389 biens) est concentré Côte d'Azur (06/83), Provence (13/84),
littoral breton (56/44) et Paris (75/92/78). Sur les 11 départements cibles
(Sarthe + couronne Centre/Val-de-Loire/Pays-de-la-Loire), le stock est quasi nul
(échantillon de ~140 fiches récentes/réparties : 1 seul bien, en 49). Comme il n'y a
pas de filtre serveur, atteindre ce stock résiduel imposerait de télécharger les
~3389 fiches détail à chaque run — non rentable. Conservé en `actif: false`.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.proprietesdecharme.com"
# Archive "Maisons & Villas de luxe" (exclut d'emblée appartements/terrains/commerces)
ARCHIVE_URL = f"{BASE_URL}/property-type/_maison/"
MAX_PAGES = 50          # plafond de sécurité (archive réelle ~277 pages — non parcourue en entier)
DETAIL_CONCURRENCY = 8
PHOTOS_PER_CARD = 10


_EXCLUDE_KEYWORDS = re.compile(r"appartement|studio|\bterrain\b|garage|parking|bureau", re.IGNORECASE)
_TYPE_MAP = [
    (re.compile(r"château", re.IGNORECASE), "château"),
    (re.compile(r"manoir", re.IGNORECASE), "manoir"),
    (re.compile(r"longère|longere", re.IGNORECASE), "longère"),
    (re.compile(r"ferme|fermette", re.IGNORECASE), "ferme"),
    (re.compile(r"hôtel particulier", re.IGNORECASE), "hôtel particulier"),
    (re.compile(r"villa", re.IGNORECASE), "villa"),
    (re.compile(r"propriété|demeure|domaine", re.IGNORECASE), "propriété"),
    (re.compile(r"maison", re.IGNORECASE), "maison"),
]


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    results: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25) as client:
        cards = await _collect_cards(client)
        print(f"[ProprietesDeCharme] {len(cards)} carte(s) maison collectées")

        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def enrich(card_data: dict) -> dict | None:
            async with sem:
                return await _fetch_detail(client, card_data)

        enriched = await asyncio.gather(*[enrich(c) for c in cards])

    for bien in enriched:
        if not bien:
            continue

        cp = bien.get("code_postal") or ""
        dept = cp[:2] if len(cp) >= 2 else ""
        if departements and dept not in departements:
            continue
        bien["departement"] = dept

        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
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
        print(f"[ProprietesDeCharme] Dept {dept}: {n} annonces")
    print(f"[ProprietesDeCharme] {len(results)} annonce(s) dans les départements ciblés")

    return results


async def _collect_cards(client: httpx.AsyncClient) -> list[dict]:
    """Parcourt l'archive maisons et renvoie les métadonnées de carte (url, titre, prix, photos)."""
    cards: list[dict] = []
    seen_urls: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = ARCHIVE_URL if page == 1 else f"{ARCHIVE_URL}page/{page}/"
        try:
            r = await client.get(url)
        except Exception as e:
            print(f"[ProprietesDeCharme] Erreur page {page}: {e}")
            break
        if r.status_code != 200:
            break

        soup = BeautifulSoup(r.text, "html.parser")
        page_cards = soup.select("div.item-listing-wrap")
        if not page_cards:
            break

        new_on_page = 0
        for card in page_cards:
            data = _parse_card(card)
            if not data or data["url"] in seen_urls:
                continue
            seen_urls.add(data["url"])
            cards.append(data)
            new_on_page += 1

        if new_on_page == 0:
            break
        await asyncio.sleep(0.4)

    return cards


def _parse_card(card) -> dict | None:
    try:
        a = card.select_one('a[href*="/property/"]')
        if not a or not a.get("href"):
            return None
        url = a["href"].strip()

        title_el = card.select_one(".item-title, h2, h3")
        titre = title_el.get_text(" ", strip=True) if title_el else ""
        titre = re.sub(r"\s+", " ", titre).strip()
        if _EXCLUDE_KEYWORDS.search(titre):
            return None

        price_el = card.select_one(".item-price")
        prix = _parse_num(price_el.get_text(" ", strip=True)) if price_el else None

        id_annonce = card.get("data-hz-id") or url

        # photos depuis data-images (JSON)
        photos: list[str] = []
        raw = card.get("data-images")
        if raw:
            try:
                for img in json.loads(raw):
                    src = img.get("image")
                    if src and src.startswith("http"):
                        photos.append(src)
            except (json.JSONDecodeError, TypeError):
                pass
        photos = photos[:PHOTOS_PER_CARD]

        type_bien = "maison"
        for rx, label in _TYPE_MAP:
            if rx.search(titre):
                type_bien = label
                break

        return {
            "url": url,
            "id_annonce": id_annonce,
            "titre": titre[:150],
            "type_bien": type_bien,
            "prix": prix,
            "photos": photos,
        }
    except Exception:
        return None


async def _fetch_detail(client: httpx.AsyncClient, card: dict) -> dict | None:
    """Télécharge la fiche pour récupérer code postal / ville / surface (JSON-LD)."""
    try:
        r = await client.get(card["url"])
        if r.status_code != 200:
            return None
    except Exception:
        return None

    listing = _extract_listing_ldjson(r.text)
    code_postal = ""
    ville = ""
    surface = None
    prix = card.get("prix")

    if listing:
        addr = listing.get("address") or {}
        if isinstance(addr, dict):
            cp = str(addr.get("postalCode") or "").strip()
            if re.fullmatch(r"\d{5}", cp):
                code_postal = cp
            ville = str(addr.get("addressLocality") or "").strip()
        fs = listing.get("floorSize") or {}
        if isinstance(fs, dict):
            surface = _to_float(fs.get("value"))
        offers = listing.get("offers") or {}
        if isinstance(offers, dict) and prix is None:
            prix = _to_float(offers.get("price"))

    # Repli code postal : tout CP5 dans le HTML si JSON-LD muet
    if not code_postal:
        m = re.search(r'"postalCode"\s*:\s*"?(\d{5})', r.text)
        if m:
            code_postal = m.group(1)

    pieces, chambres = _parse_amenities(r.text)

    return {
        "source": "proprietesdecharme",
        "url": card["url"],
        "id_annonce": card["id_annonce"],
        "titre": card["titre"],
        "type_bien": card["type_bien"],
        "description": None,
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville,
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "dpe": None,
        "photos": card.get("photos", []),
        "agence": "Propriétés De Charme",
    }


def _extract_listing_ldjson(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    for sc in soup.select('script[type="application/ld+json"]'):
        txt = sc.string or sc.get_text() or ""
        if "RealEstateListing" not in txt:
            continue
        try:
            data = json.loads(txt)
        except json.JSONDecodeError:
            continue
        for obj in _iter_ldjson(data):
            if isinstance(obj, dict) and obj.get("@type") == "RealEstateListing":
                return obj
    return None


def _iter_ldjson(data):
    if isinstance(data, list):
        for x in data:
            yield from _iter_ldjson(x)
    elif isinstance(data, dict):
        yield data
        if "@graph" in data:
            yield from _iter_ldjson(data["@graph"])


def _parse_amenities(html: str) -> tuple[int | None, int | None]:
    """Houzez : 'N chambres', 'N pièces' dans le bloc détails."""
    pieces = chambres = None
    m = re.search(r"(\d+)\s*pi[èe]ces?", html, re.IGNORECASE)
    if m:
        pieces = int(m.group(1))
    m = re.search(r"(\d+)\s*chambres?", html, re.IGNORECASE)
    if m:
        chambres = int(m.group(1))
    return pieces, chambres


def _parse_num(text: str) -> float | None:
    cleaned = re.sub(r"[^\d,\.]", "", (text or "").replace("\xa0", " ").replace(" ", ""))
    cleaned = cleaned.replace(",", ".")
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _to_float(v) -> float | None:
    try:
        return float(v) if v is not None and str(v).strip() != "" else None
    except (ValueError, TypeError):
        return None


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
    print(f"\nTotal Propriétés De Charme (depts cibles): {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€ — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

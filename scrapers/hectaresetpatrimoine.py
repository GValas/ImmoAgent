"""scrapers/hectaresetpatrimoine.py — Hectares et Patrimoine

Méthode : scrape_simple (httpx) — SSR HTML (catalogue Orisha/Apimo, encodage ISO-8859-1).

Spécialiste des propriétés rurales / équestres : moulins, manoirs, longères,
domaines agricoles, haras, maisons de pays. Inventaire NATIONAL faible (~64 annonces).

Listing : https://www.hectaresetpatrimoine.fr/annonces/transaction/vente.html
          pagination : /annonces/transaction_____{N}/vente.html
Cards   : div.listing-item  (chaque bien apparaît 2x : carte liste + pop-up → dédup par id)
  - URL/titre  : a.product-image[href]  → ../fiches/{codes}_{ID}/{slug}.html
  - id         : data-productid sur .add-fav  /  ID dans l'URL fiche
  - nom+ville  : .product-name > span (1er = nom du bien, dernier = ville)
  - prix       : .product-price  ("780 000 €")
  - pièces     : .data-list__item--NbPiece .data-list__item--value
  - surface    : .data-list__item--Surface .data-list__item--value
  - réf        : .data-list__item--products_model .data-list__item--value
  - photos     : img.photo / img.photo-hidden[src]

PAS de filtre département serveur (le seul filtre du site est par commune exacte,
dropdown C_65 "CP VILLE"). PAS de code postal sur la carte liste.
→ Stratégie : on scrape tout le listing national, puis on récupère le CODE POSTAL
  sur la PAGE FICHE de chaque bien ("Code postal</div>...<b>NNNNN</b>") et on
  POST-FILTRE par code_postal[:2] (comme groupe_mercure). Volume faible → coût OK.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.hectaresetpatrimoine.fr"
LISTING_URL = f"{BASE_URL}/annonces/transaction/vente.html"
MAX_PAGES = 12           # plafond de sécurité (~3 pages réelles)
PHOTOS_PER_CARD = 4
DETAIL_CONCURRENCY = 6   # requêtes fiches simultanées (récup CP)


# Types à exclure (on garde maisons/propriétés/manoirs/moulins/domaines…)
_EXCLUDE_TYPE = re.compile(
    r"\bterrain\b|\bappartement\b|\bgarage\b|\bparking\b|\bbureau\b|"
    r"\bfonds de commerce\b|\blocal commercial\b",
    re.IGNORECASE,
)
_TYPE_MAP = [
    (re.compile(r"manoir|château|chateau", re.IGNORECASE), "manoir"),
    (re.compile(r"moulin", re.IGNORECASE), "moulin"),
    (re.compile(r"longère|longere", re.IGNORECASE), "longère"),
    (re.compile(r"domaine", re.IGNORECASE), "domaine"),
    (re.compile(r"haras|équestre|equestre", re.IGNORECASE), "propriété équestre"),
    (re.compile(r"ferme|exploitation|agricole", re.IGNORECASE), "ferme"),
    (re.compile(r"propriété|propriete", re.IGNORECASE), "propriété"),
    (re.compile(r"maison", re.IGNORECASE), "maison"),
]


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        cards = await _fetch_all_cards(client)

        # Parse cartes (dédup par id)
        parsed: dict[str, dict] = {}
        for card in cards:
            bien = _parse_card(card)
            if not bien:
                continue
            parsed.setdefault(bien["id_annonce"], bien)

        biens = list(parsed.values())

        # Récupère le code postal sur les fiches (en parallèle, borné)
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def enrich(b: dict):
            async with sem:
                cp = await _fetch_code_postal(client, b["url"])
                if cp:
                    b["code_postal"] = cp
                    b["departement"] = cp[:2]

        await asyncio.gather(*(enrich(b) for b in biens))

    # POST-FILTRE département + prix/surface
    results: list[dict] = []
    for b in biens:
        cp = b.get("code_postal") or ""
        dept = cp[:2] if len(cp) >= 2 else ""
        if departements and dept not in departements:
            continue

        p = b.get("prix") or 0
        s = b.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        results.append(b)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[HectaresEtPatrimoine] Dept {dept}: {n} annonces")

    return results


async def _fetch_all_cards(client: httpx.AsyncClient) -> list:
    cards = []
    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = LISTING_URL
        else:
            url = f"{BASE_URL}/annonces/transaction_____{page}/vente.html"
        try:
            r = await client.get(url)
            if r.status_code != 200:
                break
        except Exception as e:
            print(f"[HectaresEtPatrimoine] Erreur page {page}: {e}")
            break

        html = r.content.decode("iso-8859-1", errors="replace")
        page_cards = BeautifulSoup(html, "html.parser").select("div.listing-item")
        if not page_cards:
            break
        cards.extend(page_cards)

        # Pas de page suivante → stop
        if not re.search(rf"transaction_____{page + 1}/vente\.html", html):
            break

        await asyncio.sleep(0.4)

    return cards


def _parse_card(card) -> dict | None:
    a = card.select_one("a.product-image[href]") or card.select_one("a.flex-grow-1[href]")
    if not a:
        return None
    href = a.get("href", "").strip()
    if not href:
        return None
    url = _abs_url(href)

    # id annonce : data-productid ou ID dans l'URL ../fiches/{codes}_{ID}/...
    id_annonce = None
    fav = card.select_one("[data-productid]")
    if fav:
        id_annonce = fav.get("data-productid")
    if not id_annonce:
        m = re.search(r"_(\d+)/", href)
        if m:
            id_annonce = m.group(1)
    if not id_annonce:
        id_annonce = url

    # nom + ville : spans dans .product-name (1er span = nom, dernier = ville)
    name_el = card.select_one(".product-name")
    titre, ville = "", ""
    if name_el:
        spans = [s.get_text(strip=True) for s in name_el.select("span")]
        spans = [s for s in spans if s and s != ","]
        if spans:
            titre = spans[0]
            if len(spans) > 1:
                ville = spans[-1]
    if not titre:
        titre = a.get("title", "").strip()

    # type de bien depuis le titre
    type_bien = "maison"
    if _EXCLUDE_TYPE.search(titre):
        return None
    for rx, label in _TYPE_MAP:
        if rx.search(titre):
            type_bien = label
            break

    # prix
    price_el = card.select_one(".product-price")
    prix = None
    if price_el:
        # retire le span honoraires éventuel
        txt = price_el.find(string=True, recursive=False)
        prix = _parse_num(txt or price_el.get_text(" ", strip=True))

    # pièces / surface / réf via data-list
    pieces = _data_list_int(card, "NbPiece")
    surface = _data_list_float(card, "Surface")
    ref = _data_list_value(card, "products_model")

    # photos
    photos = []
    for img in card.select("img.photo, img.photo-hidden"):
        src = img.get("src") or ""
        if src and not src.startswith("data:"):
            photos.append(_abs_url(src))
    # dédup en gardant l'ordre
    seen = set()
    photos = [p for p in photos if not (p in seen or seen.add(p))][:PHOTOS_PER_CARD]

    return {
        "source": "hectaresetpatrimoine",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": None,
        "departement": "",       # rempli depuis la fiche (CP)
        "ville": ville[:80],
        "code_postal": "",        # rempli depuis la fiche
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "dpe": None,
        "photos": photos,
        "agence": "Hectares et Patrimoine",
        "reference": ref or None,
    }


async def _fetch_code_postal(client: httpx.AsyncClient, url: str) -> str | None:
    """Récupère le code postal sur la page fiche."""
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return None
    except Exception:
        return None
    html = r.content.decode("iso-8859-1", errors="replace")
    # "Code postal</div><div ...><b>NNNNN</b>"
    m = re.search(r"Code postal\s*</div>\s*<div[^>]*>\s*<b>\s*(\d{5})\s*</b>", html, re.IGNORECASE)
    if m:
        return m.group(1)
    # fallback : <b>NNNNN</b> proche du libellé
    m2 = re.search(r"Code postal.{0,80}?(\d{5})", html, re.IGNORECASE | re.DOTALL)
    return m2.group(1) if m2 else None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _abs_url(href: str) -> str:
    if href.startswith("http"):
        return href
    href = href.lstrip(".")
    if not href.startswith("/"):
        href = "/" + href
    return BASE_URL + href


def _data_list_value(card, suffix: str) -> str | None:
    el = card.select_one(f".data-list__item--{suffix} .data-list__item--value")
    return el.get_text(strip=True) if el else None


def _data_list_int(card, suffix: str) -> int | None:
    v = _data_list_value(card, suffix)
    if not v:
        return None
    m = re.search(r"\d+", v)
    return int(m.group()) if m else None


def _data_list_float(card, suffix: str) -> float | None:
    v = _data_list_value(card, suffix)
    return _parse_num(v) if v else None


def _parse_num(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d,\.]", "", text.replace("\xa0", " ").replace(" ", ""))
    cleaned = cleaned.replace(",", ".")
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
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
        search({
            "departements": criteres.departements,
            "prix_max": criteres.prix_max,
            "prix_min": getattr(criteres, "prix_min", 0),
            "surface_min": criteres.surface_min,
        })
    )
    print(f"\nTotal Hectares et Patrimoine (depts cibles): {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€ — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p — {b['type_bien']} — {b['ville']}"
        )

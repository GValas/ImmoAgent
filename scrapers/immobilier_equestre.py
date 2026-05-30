"""scrapers/immobilier_equestre.py — Immobilier Equestre

Méthode : scrape_simple (httpx) — SSR HTML WordPress (plugin equirodi-immo).

Site MONO-AGENCE (Valentine Immobilier) spécialisé propriétés équestres :
haras, propriétés normandes, maisons en colombages, longères. Inventaire NATIONAL
très faible (~16 annonces) et géographiquement concentré en NORMANDIE (depts 14,
61, 76, 27, 50) — agence basée en Pays d'Auge.

Listing : https://www.immobilier-equestre.com/proprietes/
          pagination : /proprietes/page/{N}/  (12 cartes/page, ~2 pages)
Cards   : div.listing-wrap
  - URL/titre : h3.listing-title a[href]  → /proprietes/{slug}/
  - réf+région: .listing-ref  ("Réf : VM572-... - Basse-Normandie")
  - prix      : span.list-price  ("499 000 €")
  - description: .list-desc

PAS de code postal / ville / surface sur la carte liste.
→ Stratégie : on scrape le listing, puis on récupère localisation + surface sur la
  PAGE FICHE de chaque bien et on POST-FILTRE par code_postal[:2] (comme
  hectaresetpatrimoine / groupe_mercure). Volume faible → coût OK.

Sur la fiche, le 1er ul.listings-datas contient : prix / "{CP} {Ville}" / région ;
le 2e ul.listings-datas contient : "Surface habitable : N m²" / "Nombre de pièces : N"
/ "Nombre de chambres : N".

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.immobilier-equestre.com"
LISTING_URL = f"{BASE_URL}/proprietes/"
MAX_PAGES = 8            # plafond de sécurité (~2 pages réelles)
DETAIL_CONCURRENCY = 6   # requêtes fiches simultanées

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types à exclure (on garde maisons/propriétés/manoirs/longères/haras…)
_EXCLUDE_TYPE = re.compile(
    r"\bterrain\b|\bappartement\b|\bgarage\b|\bparking\b|\bbureau\b|"
    r"\bfonds de commerce\b|\blocal commercial\b",
    re.IGNORECASE,
)
_TYPE_MAP = [
    (re.compile(r"manoir|château|chateau", re.IGNORECASE), "manoir"),
    (re.compile(r"longère|longere|colombages", re.IGNORECASE), "longère"),
    (re.compile(r"moulin", re.IGNORECASE), "moulin"),
    (re.compile(r"domaine", re.IGNORECASE), "domaine"),
    (re.compile(r"haras|équestre|equestre", re.IGNORECASE), "propriété équestre"),
    (re.compile(r"ferme|exploitation|agricole|bâtiment", re.IGNORECASE), "ferme"),
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

        parsed: dict[str, dict] = {}
        for card in cards:
            bien = _parse_card(card)
            if not bien:
                continue
            parsed.setdefault(bien["url"], bien)

        biens = list(parsed.values())

        # Récupère localisation + surface/pièces sur les fiches (en parallèle, borné)
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def enrich(b: dict):
            async with sem:
                await _fetch_detail(client, b)

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
        print(f"[ImmobilierEquestre] Dept {dept}: {n} annonces")

    return results


async def _fetch_all_cards(client: httpx.AsyncClient) -> list:
    cards = []
    for page in range(1, MAX_PAGES + 1):
        url = LISTING_URL if page == 1 else f"{BASE_URL}/proprietes/page/{page}/"
        try:
            r = await client.get(url)
            if r.status_code != 200:
                break
        except Exception as e:
            print(f"[ImmobilierEquestre] Erreur page {page}: {e}")
            break

        page_cards = BeautifulSoup(r.text, "html.parser").select("div.listing-wrap")
        if not page_cards:
            break
        cards.extend(page_cards)
        await asyncio.sleep(0.3)

    return cards


def _parse_card(card) -> dict | None:
    a = card.select_one("h3.listing-title a[href]")
    if not a:
        return None
    href = a.get("href", "").strip()
    if not href:
        return None
    url = _abs_url(href)
    titre = a.get_text(strip=True)

    if _EXCLUDE_TYPE.search(titre):
        return None
    type_bien = "propriété"
    for rx, label in _TYPE_MAP:
        if rx.search(titre):
            type_bien = label
            break

    price_el = card.select_one(".list-price")
    prix = _parse_num(price_el.get_text(" ", strip=True)) if price_el else None

    desc_el = card.select_one(".list-desc")
    description = desc_el.get_text(" ", strip=True) if desc_el else None

    ref_el = card.select_one(".listing-ref")
    ref = None
    if ref_el:
        m = re.search(r"Réf\s*:\s*([^\-]+)", ref_el.get_text(" ", strip=True))
        if m:
            ref = m.group(1).strip()

    return {
        "source": "immobilier_equestre",
        "url": url,
        "id_annonce": href.rstrip("/").split("/")[-1],
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description,
        "departement": "",       # rempli depuis la fiche
        "ville": "",             # rempli depuis la fiche
        "code_postal": "",       # rempli depuis la fiche
        "surface": None,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "dpe": None,
        "photos": [],
        "agence": "Immobilier Equestre",
        "reference": ref,
    }


async def _fetch_detail(client: httpx.AsyncClient, b: dict) -> None:
    """Récupère localisation (CP/ville) + surface/pièces/chambres + photo sur la fiche."""
    try:
        r = await client.get(b["url"])
        if r.status_code != 200:
            return
    except Exception:
        return
    soup = BeautifulSoup(r.text, "html.parser")

    for ul in soup.select("ul.listings-datas"):
        items = [li.get_text(" ", strip=True) for li in ul.select("li")]
        for it in items:
            # "{CP} {Ville}"
            m = re.match(r"(\d{5})\s+(.+)", it)
            if m and not b["code_postal"]:
                b["code_postal"] = m.group(1)
                b["departement"] = m.group(1)[:2]
                b["ville"] = m.group(2).strip()[:80]
            # "Surface habitable : N m²"
            ms = re.search(r"Surface habitable\s*:\s*([\d\s.,]+)\s*m", it, re.IGNORECASE)
            if ms and b["surface"] is None:
                b["surface"] = _parse_num(ms.group(1))
            # "Surface du terrain : N m²" / "Terrain : N m²"
            mt = re.search(r"(?:Surface du terrain|Terrain)\s*:\s*([\d\s.,]+)\s*m",
                           it, re.IGNORECASE)
            if mt and b["surface_terrain"] is None:
                b["surface_terrain"] = _parse_num(mt.group(1))
            mp = re.search(r"Nombre de pièces\s*:\s*(\d+)", it, re.IGNORECASE)
            if mp and b["pieces"] is None:
                b["pieces"] = int(mp.group(1))
            mc = re.search(r"Nombre de chambres\s*:\s*(\d+)", it, re.IGNORECASE)
            if mc and b["chambres"] is None:
                b["chambres"] = int(mc.group(1))

    # photo principale
    img = soup.select_one("img.wp-post-image, .listing-widget-thumb img, .property-gallery img")
    if img:
        src = img.get("src") or ""
        if src and not src.startswith("data:"):
            b["photos"] = [_abs_url(src)]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _abs_url(href: str) -> str:
    if href.startswith("http"):
        return href
    if not href.startswith("/"):
        href = "/" + href
    return BASE_URL + href


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
    print(f"\nTotal Immobilier Equestre (depts cibles): {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€ — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p — {b['type_bien']} — {b['ville']}"
        )

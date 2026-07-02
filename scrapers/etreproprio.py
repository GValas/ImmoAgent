"""scrapers/etreproprio.py — EtreProprio (agrégateur national d'annonces d'agences)

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /maison-a-vendre/{NN}      (ex: /maison-a-vendre/72)
              → filtre département CÔTÉ SERVEUR par numéro de dept (tout dept FR).
              La liste rend 60 cartes SSR ; pas de pagination httpx fiable
              (chargement AJAX/scroll) → on exploite les 60 cartes de tête.

Cartes liste : a.ep-card-cla-a (href = page détail)
  - URL détail : href  → /immobilier-{ID}-vente-maison-...
  - ID annonce : segment immobilier-{ID}-
  - Titre      : .ep-title   →  "Maison 75 m² à Conlie"  (surface dans le titre)
  - Ville      : .ep-city
  - Prix       : .ep-price   →  "94 990 €"
  - Photo      : .ep-img img[src]
  - Agence     : .ep-rea img[title]
  - Desc       : .ep-desc (tronquée)

⚠️ La carte liste NE contient PAS le code postal. Pour garantir 0 fuite
hors-département (le filtre serveur /NN n'est pas vérifiable autrement), on
récupère la page détail de chaque bien : elle embarque un blob JS contenant
"postalCode":"NNNNN","departmentCode":"NN" + price/houseArea/terrainArea/roomNb/
dpeGlobalLetter. On post-filtre STRICT sur code_postal[:2] == dept (les biens
sans CP en détail sont écartés — puis garde-fou `keep_bien` du driver).

Pour rester poli et borné, on limite à MAX_CARDS biens/dept et on récupère les
pages détail avec une concurrence plafonnée.

Migré sur scrapers/_base.py : boucle départements, dédup, garde-fou CP et
filtres prix/surface sont fournis par `run_dept_api` ; ce fichier ne porte plus
que la récupération liste + enrichissement détail propres à EtreProprio.

Couverture : agrégateur national (mandataires + agences), gros stock par dept
cible (3000+ maisons en Sarthe). On ne garde que les maisons (segment d'URL).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, run_dept_api, standalone_main
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.etreproprio.com"
MAX_CARDS = 60          # cartes SSR rendues par page liste (pas de pagination httpx)
DETAIL_CONCURRENCY = 6  # requêtes détail simultanées (politesse)


async def search(criteres: dict) -> list[dict]:
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)

    async def _fetch_dept(client, dept, slug):
        return await _scrape_dept(client, dept, prix_max, prix_min)

    return await run_dept_api(
        source="etreproprio",
        label="EtreProprio",
        fetch_dept=_fetch_dept,
        criteres=criteres,
        dept_sleep=0.6,
        client_kwargs={"timeout": 25},
    )


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    prix_max: int,
    prix_min: int,
) -> list[dict]:
    r = await get_with_retry(client, f"{BASE_URL}/maison-a-vendre/{dept}")
    if r is None or r.status_code != 200:
        if r is not None:
            print(f"[EtreProprio] Dept {dept}: HTTP {r.status_code}")
        return []

    cards = BeautifulSoup(r.text, "html.parser").select("a.ep-card-cla-a")[:MAX_CARDS]
    if not cards:
        return []

    # Pré-parse des cartes (titre/ville/prix/photo/url/id) ; pré-filtre prix
    # liste pour éviter de récupérer des pages détail inutiles.
    stubs: list[dict] = []
    seen_ids: set[str] = set()
    for card in cards:
        stub = _parse_card(card)
        if not stub:
            continue
        if stub["id_annonce"] in seen_ids:
            continue
        p = stub.get("prix") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        seen_ids.add(stub["id_annonce"])
        stubs.append(stub)

    # Enrichissement via pages détail (CP/dept/terrain/pièces/dpe) avec concurrence bornée
    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def enrich(stub: dict) -> dict | None:
        async with sem:
            rd = await get_with_retry(client, stub["url"])
            await asyncio.sleep(0.2)
            if rd is None or rd.status_code != 200:
                return None
            return _merge_detail(stub, rd.text, dept)

    enriched = await asyncio.gather(*(enrich(s) for s in stubs))

    # Biens sans CP en détail ⇒ écartés (le garde-fou dept du driver exige un CP
    # vérifiable pour garantir 0 fuite) ; le reste est filtré par keep_bien.
    return [b for b in enriched if b and b["code_postal"]]


def _parse_card(card) -> dict | None:
    href = card.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    m = re.search(r"immobilier-(\d+)-", href)
    id_annonce = m.group(1) if m else url

    title_el = card.select_one(".ep-title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    city_el = card.select_one(".ep-city")
    ville = city_el.get_text(" ", strip=True) if city_el else ""

    price_el = card.select_one(".ep-price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    desc_el = card.select_one(".ep-desc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    rea_el = card.select_one(".ep-rea img")
    agence = (rea_el.get("title") or rea_el.get("alt")) if rea_el else None

    photos = []
    img = card.select_one(".ep-img img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)

    # Surface depuis le titre ("Maison 75 m² à ...")
    surface = _parse_surface_title(titre)

    return {
        "source": "etreproprio",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": description[:1200],
        "departement": None,
        "ville": _titlecase(ville)[:80],
        "code_postal": "",
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": agence,
    }


def _merge_detail(stub: dict, html: str, dept: str) -> dict:
    """Complète le stub avec le blob JS de la page détail (CP/dept/terrain/pièces/dpe)."""
    bien = dict(stub)

    # postalCode + departmentCode appariés (évite l'adresse du siège dans le JSON-LD Organization)
    m_cp = re.search(r'"postalCode":"(\d{5})","departmentCode":"(\d{2,3})"', html)
    if m_cp:
        bien["code_postal"] = m_cp.group(1)
        bien["departement"] = m_cp.group(2)
    else:
        # secours : departmentCode seul (n'apparaît que dans le blob annonce)
        m_d = re.search(r'"departmentCode":"(\d{2,3})"', html)
        if m_d:
            bien["departement"] = m_d.group(1)

    if bien.get("departement") is None:
        bien["departement"] = dept

    # Enrichissements numériques
    ha = _blob_num(html, "houseArea")
    if ha:
        bien["surface"] = ha
    ta = _blob_num(html, "terrainArea")
    if ta:
        bien["surface_terrain"] = ta
    rn = _blob_int(html, "roomNb")
    if rn:
        bien["pieces"] = rn
    pr = _blob_int(html, "price")
    if pr and not bien.get("prix"):
        bien["prix"] = float(pr)

    m_dpe = re.search(r'"dpeGlobalLetter":"([A-Ga-g])"', html)
    if m_dpe:
        bien["dpe"] = m_dpe.group(1).upper()

    return bien


# ── Helpers ──────────────────────────────────────────────────────────────────

def _blob_num(html: str, key: str) -> float | None:
    m = re.search(r'"' + key + r'":"?([\d.]+)"?', html)
    if m:
        try:
            v = float(m.group(1))
            return v if v > 0 else None
        except ValueError:
            return None
    return None


def _blob_int(html: str, key: str) -> int | None:
    m = re.search(r'"' + key + r'":"?(\d+)"?', html)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _parse_surface_title(text: str) -> float | None:
    """'Maison 75 m² à Conlie' → 75.0"""
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0]*)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 3000:
                return f
        except ValueError:
            pass
    return None


def _titlecase(s: str) -> str:
    """'YVRE-LE-POLIN' → 'Yvre-Le-Polin'"""
    if not s:
        return s
    return "-".join(p.capitalize() for p in s.lower().split("-"))


if __name__ == "__main__":
    standalone_main(search, "EtreProprio")

"""scrapers/france_chateau_propriete.py — France Château Propriété (Agence GTI Chassagne)

Agence spécialisée châteaux / manoirs / propriétés de caractère, ancrée Sud-Ouest
(Périgord, Lot, Limousin) → en pratique 0 stock en zone cible (Centre/Val-de-Loire),
mais scraper fonctionnel et 0 fuite garantie.

Méthode : scrape_simple (httpx) — SSR HTML (CMS Activimmo). httpx pur 200, pas de
Playwright.

URL liste  : /index.php?page={N}&action=list   (~25 pages, 9 cartes/page)
URL détail : /index.php?action=detail&nbien={ID}

Cartes (liste) : div.post
  - URL/titre : .post-title h3 a[href]   (href = ../../index.php?action=detail&nbien=ID)
  - Prix      : .post-title h4           ("449.000 €")
  - Type+ville+infos : .post-meta        ("Maison Contemporaine - Les Eyzies -
                Réf.:MP114070 - 180 m² - 5000 m²")  → type, ville, surface, terrain
  - Pièces/terrain : .picto span.chambre / .terrain
  → la liste N'EXPOSE PAS de code postal, seulement le nom de ville.

Filtre département : pas de code postal en liste. On filtre d'abord prix/surface
(réduit fortement le nombre de candidats), puis on résout la ville en code postal
via l'API BAN officielle (api-adresse.data.gouv.fr, type=municipality, citycode →
préfixe dept), avec cache mémoire. Post-filtre STRICT : ville indéterminée ou
département hors-zone → bien EXCLU. → 0 fuite.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.francechateaupropriete.com"
LIST_URL = f"{BASE_URL}/index.php?page={{page}}&action=list"
MAX_PAGES = 30
PHOTOS_PER_CARD = 10

BAN_URL = "https://api-adresse.data.gouv.fr/search/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|long[èe]re|manoir|ch[âa]teau|moulin|"
    r"demeure|domaine|mas|bastide|gentilhommi|corps de ferme|g[îi]te|grange",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|viager|studio",
    re.IGNORECASE,
)

# Cache mémoire : ville (normalisée) -> code_postal résolu (str | None)
_CP_CACHE: dict[str, str | None] = {}


async def _ville_to_cp(client: httpx.AsyncClient, ville: str) -> str | None:
    key = ville.strip().lower()
    if not key:
        return None
    if key in _CP_CACHE:
        return _CP_CACHE[key]
    cp: str | None = None
    try:
        r = await client.get(
            BAN_URL, params={"q": ville, "type": "municipality", "limit": 1},
            timeout=15,
        )
        if r.status_code == 200:
            feats = r.json().get("features", [])
            if feats:
                props = feats[0].get("properties", {})
                cp = props.get("postcode") or None
                citycode = props.get("citycode") or ""
                if cp and citycode[:2] != cp[:2]:
                    cp = citycode[:2] + cp[2:] if len(cp) == 5 else cp
    except Exception:
        cp = None
    _CP_CACHE[key] = cp
    return cp


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    candidats: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1. Crawl complet + filtre prix/surface/type (sans dept encore)
        for page in range(1, MAX_PAGES + 1):
            try:
                r = await client.get(LIST_URL.format(page=page))
            except Exception as e:
                print(f"[FCP] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break
            cards = BeautifulSoup(r.text, "html.parser").select("div.post")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                bien = _parse_card(card)
                if not bien:
                    continue
                if bien["id_annonce"] in seen_ids:
                    continue
                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue
                seen_ids.add(bien["id_annonce"])
                candidats.append(bien)
                new_on_page += 1
            print(f"[FCP] Page {page}: {new_on_page} candidats (prix/surface/type OK)")
            await asyncio.sleep(0.5)

        # 2. Résolution ville → CP → post-filtre dept STRICT
        results: list[dict] = []
        for b in candidats:
            ville = b.get("ville") or ""
            cp = await _ville_to_cp(client, ville) if ville else None
            dept = cp[:2] if cp else ""
            if not dept or dept not in departements:
                continue  # indéterminé / hors-zone → exclu (0 fuite)
            b["code_postal"] = cp
            b["departement"] = dept
            results.append(b)

    print(f"[FCP] Total : {len(results)} biens (zone cible)")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one(".post-title h3 a[href], .post-title a[href]")
    href = link.get("href", "") if link else ""
    if not href or "nbien=" not in href:
        return None
    url = _abs(href)
    m = re.search(r"nbien=(\d+)", href)
    id_annonce = m.group(1) if m else url

    title_el = card.select_one(".post-title h3")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    price_el = card.select_one(".post-title h4")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # .post-meta : "<Type> - <Ville> - Réf.:XXX - <surf> m² - <terrain> m²"
    meta_el = card.select_one(".post-meta")
    type_bien = ""
    ville = ""
    if meta_el:
        links = meta_el.select("h4 a")
        if links:
            type_bien = links[0].get_text(" ", strip=True)
        if len(links) > 1:
            ville = links[1].get_text(" ", strip=True)
    meta_txt = meta_el.get_text(" ", strip=True) if meta_el else ""

    blob = f"{type_bien} {titre}"
    if _EXCLUDE_TYPE.search(blob) and not _KEEP_TYPE.search(blob):
        return None

    # Surface habitable : 1er "NNN m²" du meta (avant le terrain)
    surface = None
    terrain = None
    m2 = re.findall(r"([\d\s.,]+?)\s*m²", meta_txt)
    if m2:
        surface = _num(m2[0])
        if len(m2) > 1:
            terrain = _num(m2[1])

    # picto : chambres / terrain plus fiable
    chambres = None
    picto = card.select_one(".picto")
    if picto:
        ch = picto.select_one(".chambre")
        if ch:
            chambres = _to_int(ch.get_text(strip=True))
        tr = picto.select_one(".terrain")
        if tr:
            t = _num(tr.get_text(strip=True))
            if t:
                terrain = t

    photos: list[str] = []
    img = card.select_one(".img-wr img, .img-hold img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(_abs(src))

    return {
        "source": "france_chateau_propriete",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": (type_bien or "propriété")[:60],
        "description": "",
        "departement": "",
        "ville": ville[:80],
        "code_postal": None,
        "surface": surface,
        "surface_terrain": terrain,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "France Château Propriété (GTI Chassagne)",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _abs(href: str) -> str:
    if href.startswith("http"):
        return href
    href = href.lstrip(".")
    if not href.startswith("/"):
        href = "/" + href
    return BASE_URL + href


def _parse_price(text: str) -> float | None:
    # "449.000 €" : le point est un séparateur de milliers FR
    cleaned = re.sub(r"[€\s\xa0]", "", text or "")
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _num(text: str) -> float | None:
    """'5.000m²' / '5 000' / '180' → 5000.0 / 5000.0 / 180.0 (point=milliers)."""
    raw = re.sub(r"[^\d.,\s]", "", text or "")
    raw = re.sub(r"[\s.,]", "", raw)   # point & virgule = séparateurs milliers ici
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _to_int(text: str) -> int | None:
    m = re.search(r"(\d+)", text or "")
    return int(m.group(1)) if m else None


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
    print(f"\nTotal France Château Propriété: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:45]}"
            f" — {b['prix']}€ — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m² — {b['ville']}"
        )

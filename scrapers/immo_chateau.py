"""scrapers/immo_chateau.py — Immobilière du Château (Moulins-Engilbert, Morvan/Nièvre 58)

Agence du Sud-Nivernais / Bazois / Morvan → couvre surtout la Nièvre (58, IN ZONE)
et quelques marges Saône-et-Loire (71, hors zone).

Méthode : scrape_simple (httpx) — SSR HTML (WordPress, thème WPResidence,
custom post type `estate_property`). httpx pur 200, pas de Playwright.

URL liste  : /properties-list/              (page 1)
             /properties-list/page/{N}/      (page N, ~14 pages, 18 cartes/page)

Cartes (liste) : div.property_unit_type4
  - URL/titre : .property-unit-information-wrapper h4 a[href]  (→ /estate_property/{slug}/)
  - data-listid : id_annonce
  - Chambres  : .inforoom_unit_type4    ("5 Chambres")
  - Surface   : .infosize_unit_type4    ("Surface 242.00 m²")
  - Prix      : .propery_price4_grid    ("329 500 € F.A.I.")
  - Photos    : .carousel-item img[src]
  → la liste N'EXPOSE PAS la ville ni le code postal.

Filtre département : la liste ne porte aucune localisation ; la page détail expose
seulement le NOM de la commune (.property_categs, ex "CHARRIN"), sans code postal
(le seul CP du HTML détail est celui de l'agence de Moulins-Engilbert, 58290).
On enrichit donc chaque survivant prix/surface en page détail (commune + terrain +
pièces + DPE), puis on résout la commune en code postal via l'API BAN officielle
(api-adresse.data.gouv.fr, type=municipality, citycode → préfixe département).
Post-filtre STRICT : on ne garde que les biens dont le dept résolu ∈ cibles.
Commune indéterminée ou hors-zone → bien EXCLU. → 0 fuite.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.immo-chateau.fr"
LIST_URL_P1 = f"{BASE_URL}/properties-list/"
LIST_URL_PN = f"{BASE_URL}/properties-list/page/{{page}}/"
MAX_PAGES = 16
PHOTOS_PER_CARD = 10
DETAIL_CONCURRENCY = 4

BAN_URL = "https://api-adresse.data.gouv.fr/search/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain (?:à bâtir|constructible)|local commercial|garage|parking|"
    r"immeuble de rapport|bureau|fonds de commerce",
    re.IGNORECASE,
)

# Cache mémoire : commune (normalisée) -> code_postal résolu (str | None)
_CP_CACHE: dict[str, str | None] = {}


async def _commune_to_cp(client: httpx.AsyncClient, commune: str) -> str | None:
    """Résout un nom de commune en code postal via l'API BAN (cache mémoire)."""
    key = commune.strip().lower()
    if not key:
        return None
    if key in _CP_CACHE:
        return _CP_CACHE[key]
    cp: str | None = None
    try:
        r = await client.get(
            BAN_URL, params={"q": commune, "type": "municipality", "limit": 1},
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
        # 1. Liste : parse + filtre prix/surface (la localisation viendra du détail)
        for page in range(1, MAX_PAGES + 1):
            url = LIST_URL_P1 if page == 1 else LIST_URL_PN.format(page=page)
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[ImmoChateau] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break
            cards = BeautifulSoup(r.text, "html.parser").select("div.property_unit_type4")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                bien = _parse_card(card)
                if not bien:
                    continue
                # dédup sur l'URL : le thème WPResidence rend chaque carte 2×
                # (vue grille + vue liste), seule l'une porte data-listid.
                if bien["url"] in seen_ids:
                    continue
                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue
                seen_ids.add(bien["url"])
                candidats.append(bien)
                new_on_page += 1
            print(f"[ImmoChateau] Page {page}: {new_on_page} candidats (prix/surface OK)")
            await asyncio.sleep(0.5)

        # 2. Enrichissement détail (commune, terrain, pièces, DPE, description)
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def enrich(b: dict):
            async with sem:
                try:
                    await _enrich_detail(client, b)
                except Exception as e:
                    print(f"[ImmoChateau] Erreur détail {b['id_annonce']}: {e}")
                await asyncio.sleep(0.3)

        await asyncio.gather(*(enrich(b) for b in candidats))

        # 3. Résolution commune → code postal → post-filtre dept STRICT
        results: list[dict] = []
        for b in candidats:
            commune = b.pop("_commune", "") or b.get("ville") or ""
            cp = await _commune_to_cp(client, commune) if commune else None
            dept = cp[:2] if cp else ""
            if not dept or dept not in departements:
                continue  # indéterminé ou hors-zone → exclu (0 fuite)
            b["code_postal"] = cp
            b["departement"] = dept
            b["ville"] = commune.title()[:80]
            results.append(b)

    print(f"[ImmoChateau] Total : {len(results)} biens (zone cible)")
    return results


def _parse_card(card) -> dict | None:
    info = card.select_one(".property-unit-information-wrapper")
    if not info:
        return None
    link = info.select_one("h4 a[href]")
    href = link.get("href", "") if link else ""
    if not href or "estate_property" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    id_annonce = card.get("data-listid") or url

    title_el = info.select_one("h4 a")
    titre = title_el.get("title") or title_el.get_text(" ", strip=True) if title_el else ""
    # le titre liste est tronqué ("...") ; le détail le complétera
    titre = re.sub(r"\s*\.\.\.$", "", titre).strip()

    if _EXCLUDE_TYPE.search(titre):
        return None

    chambres = _first_int(info.select_one(".inforoom_unit_type4"))
    sdb = _first_int(info.select_one(".infobath_unit_type4"))  # noqa: F841
    surface = None
    size_el = info.select_one(".infosize_unit_type4")
    if size_el:
        surface = _surface_m2(size_el.get_text(" ", strip=True))

    price_el = info.select_one(".propery_price4_grid")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    desc_el = info.select_one(".listing_details")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""
    description = re.sub(r"\s*\.\.\.$", "", description).strip()

    photos: list[str] = []
    for img in card.select(".carousel-item img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "immo_chateau",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": "maison",          # affiné à l'enrichissement
        "description": description[:1200],
        "departement": "",
        "ville": "",
        "code_postal": None,
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Immobilière du Château",
    }


async def _enrich_detail(client: httpx.AsyncClient, b: dict) -> None:
    r = await client.get(b["url"])
    if r.status_code != 200:
        return
    soup = BeautifulSoup(r.text, "html.parser")

    # Commune : taxonomie .property_categs (ex "CHARRIN")
    cat = soup.select_one(".property_categs")
    if cat:
        commune = cat.get_text(" ", strip=True)
        # garder seulement le 1er terme (parfois "Ville, Quartier")
        commune = commune.split(",")[0].split("·")[0].strip()
        if commune:
            b["_commune"] = commune

    # Titre complet
    h1 = soup.select_one("h1")
    if h1:
        full = h1.get_text(" ", strip=True)
        if full and len(full) > len(b.get("titre", "")):
            b["titre"] = full[:150]

    # Rangées de détail .listing_detail : "Label: Valeur"
    rows: dict[str, str] = {}
    for el in soup.select(".listing_detail"):
        t = el.get_text(" ", strip=True)
        m = re.match(r"([^:]+):\s*(.+)", t)
        if m:
            rows[m.group(1).strip().lower()] = m.group(2).strip()

    for k, v in rows.items():
        if "surface du terrain" in k:
            b["surface_terrain"] = _surface_m2(v)
        elif "surface habitable" in k and not b.get("surface"):
            b["surface"] = _surface_m2(v)
        elif "nombre de pièces" in k or "pièces" in k:
            b["pieces"] = _to_int(v)
        elif k == "chambres" and not b.get("chambres"):
            b["chambres"] = _to_int(v)
        elif "dpe" in k:
            m = re.search(r"\b([A-G])\b", v)
            if m:
                b["dpe"] = m.group(1)

    # Description (panneau description complet)
    desc = soup.select_one(".panel-body, .wpestate_estate_property_design_intext_descriptions")
    if desc:
        t = desc.get_text(" ", strip=True)
        if t and len(t) > len(b.get("description", "")):
            b["description"] = t[:1200]

    # Photos additionnelles
    photos = list(b.get("photos") or [])
    for img in soup.select(".carousel-item img, .wpestate_estate_slider_image"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:") and src not in photos:
            photos.append(src)
    b["photos"] = photos[:PHOTOS_PER_CARD]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _first_int(el) -> int | None:
    if el is None:
        return None
    m = re.search(r"(\d+)", el.get_text(" ", strip=True))
    return int(m.group(1)) if m else None


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text or "")
    cleaned = re.sub(r"[^\d].*$", "", cleaned)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _to_float(s: str) -> float | None:
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _surface_m2(text: str) -> float | None:
    """'242.00 m 2' / '6,687.00 m²' → 242.0 / 6687.0 (coupe avant l'unité 'm',
    gère le séparateur de milliers ','). None si rien de plausible."""
    if not text:
        return None
    m = re.search(r"([\d][\d\s,\.]*?)\s*m", text)
    if not m:
        return None
    raw = m.group(1).strip()
    # format anglo : virgule = milliers, point = décimales (ex 6,687.00)
    if "," in raw and "." in raw:
        raw = raw.replace(",", "")
    else:
        raw = raw.replace(",", ".")
    raw = re.sub(r"\s", "", raw)
    return _to_float(raw)


def _to_int(s: str) -> int | None:
    m = re.search(r"(\d+)", s or "")
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
    print(f"\nTotal Immo Château: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b.get('chambres') or '?'}ch — {b['ville']} — DPE {b.get('dpe')}"
        )

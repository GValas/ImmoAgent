"""scrapers/h36_immo.py — 36h immo (36heures.immo, ventes interactives en ligne)

Méthode : scrape_simple (httpx) — SSR HTML (Rails/Hotwire, cartes <article>).
Niche : ventes interactives (enchères en ligne type immonot) — catalogue NATIONAL
une-page paginé, pas de filtre département côté serveur.
URL pattern : /fr/annonces/ventes-interactives-immobilieres-en-ligne.html
              puis ...-en-ligne-p-{N}.html (N ≥ 2), ~10 cartes/page.
Cartes : article contenant un lien /fr/annonce/{ULID}/{slug}.html ; la carte
expose type + « Ville (CP) » (h3), pièces/chambres/surface/terrain (spans),
photos (carrousel splide) et le prix affiché = 1re OFFRE POSSIBLE (mise à prix,
le prix final d'adjudication sera supérieur — même logique que viager bouquet).
Post-filtre STRICT code_postal[:2] ∈ départements cibles (0 fuite).

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import (
    get_with_retry,
    keep_bien,
    make_client,
    parse_int,
    parse_loc,
    parse_price_digits,
    standalone_main,
)

BASE_URL = "https://www.36heures.immo"
LIST_PATH = "/fr/annonces/ventes-interactives-immobilieres-en-ligne"
MAX_PAGES = 8
PHOTOS_PER_CARD = 10

# Types conservés (maisons / propriétés) vs exclus (appart, immeuble, fonds…)
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|longère|longere|manoir|ch[âa]teau|"
    r"moulin|demeure|domaine|mas|g[îi]te|corps de ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|immeuble|terrain|local|commerce|garage|parking|bureau|fonds",
    re.IGNORECASE,
)

_RE_PRIX = re.compile(r"([\d][\d\s\xa0]{2,12})€")
_RE_M2 = re.compile(r"([\d][\d\s\xa0]*)\s*m[²2]\b")


def _page_url(page: int) -> str:
    if page == 1:
        return f"{BASE_URL}{LIST_PATH}.html"
    return f"{BASE_URL}{LIST_PATH}-p-{page}.html"


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set = set()

    async with make_client() as client:
        for page in range(1, MAX_PAGES + 1):
            r = await get_with_retry(client, _page_url(page))
            if r is None or r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            cards = [a for a in soup.select("article")
                     if a.select_one('a[href*="/fr/annonce/"]')]
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue
                dept = str(bien.get("code_postal") or "")[:2]
                if dept not in departements:
                    continue  # catalogue national → post-filtre strict zone
                bien["departement"] = dept
                if not keep_bien(bien, dept, seen_ids, prix_max=prix_max,
                                 prix_min=prix_min, surface_min=surface_min):
                    continue
                results.append(bien)
                new_on_page += 1
            print(f"[36hImmo] Page {page}: {new_on_page} annonces en zone")
            await asyncio.sleep(0.5)

    return results


def _parse_card(card) -> dict | None:
    link = card.select_one('a[href*="/fr/annonce/"]')
    href = link.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce : ULID du chemin /fr/annonce/{ULID}/...
    m = re.search(r"/fr/annonce/([A-Z0-9]{10,})/", url)
    id_annonce = m.group(1) if m else url

    # h3 : « Maison » + span « Sarzeau (56370) »
    h3 = card.select_one("h3")
    if not h3:
        return None
    span = h3.select_one("span")
    loc_txt = span.get_text(" ", strip=True) if span else ""
    ville, code_postal = parse_loc(loc_txt)
    if not code_postal:
        return None  # sans CP, pas de garantie 0 fuite → écarté
    type_bien = h3.get_text(" ", strip=True).replace(loc_txt, "").strip()
    if _EXCLUDE_TYPE.search(type_bien) or not _KEEP_TYPE.search(type_bien):
        return None

    # Titre : aria-label du lien de la carte, sinon type + ville
    titre = (link.get("aria-label") or "").strip() or f"{type_bien} {ville}".strip()

    # Le slug est auto-descriptif et FIABLE : {type}-{ville}-{cp}-{N}p-{S}m2-{P}euros
    slug = url.rsplit("/", 1)[-1]
    slug_pieces = parse_int(r"-(\d+)p-", slug)
    slug_surface = parse_int(r"-(\d+)m2-", slug)
    slug_prix = parse_int(r"-(\d+)euros", slug)

    txt = card.get_text(" ", strip=True)

    # Prix : slug, sinon premier montant € de la carte — dans les deux cas il
    # s'agit de la « 1re offre possible » (mise à prix, honoraires inclus)
    prix = float(slug_prix) if slug_prix else None
    if prix is None:
        m = _RE_PRIX.search(txt)
        if m:
            prix = parse_price_digits(m.group(1))

    pieces = slug_pieces or parse_int(r"(\d+)\s*pi[èe]ces?", txt)
    chambres = parse_int(r"(\d+)\s*chambres?", txt)

    # Surfaces : slug pour l'habitable ; dernier « N m² » de la carte = terrain
    # (quand il diffère de l'habitable)
    m2 = [parse_price_digits(v) for v in _RE_M2.findall(txt)]
    surface = float(slug_surface) if slug_surface else (m2[0] if m2 else None)
    surface_terrain = None
    if m2 and m2[-1] != surface:
        surface_terrain = m2[-1]

    photos = []
    for img in card.select("img"):
        src = img.get("src") or img.get("data-splide-lazy") or ""
        if src and not src.startswith("data:"):
            if src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "h36_immo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien.lower(),
        "description": "",
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": None,
    }


if __name__ == "__main__":
    standalone_main(search, "36h immo")

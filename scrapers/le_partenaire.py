"""scrapers/le_partenaire.py — Le-Partenaire.fr (réseau de mandataires immobiliers indépendants)

Méthode : scrape_simple (httpx) — SSR HTML (nginx, contenu présent dans le HTML brut).

Réseau de mandataires indépendants basé à Orléans (45). Couverture inégale mais
réelle sur le grand Val-de-Loire / Berry (l'Indre 36 est bien fourni).

Stratégie filtre département (CÔTÉ SERVEUR, 0 fuite garantie) :
  1. Page annuaire départementale : /immobilier/vente/maison/{dept-slug}
     → ne liste QUE des liens villes du département (CP préfixé par le n° de dept),
       ex. /immobilier/vente/maison/chateauroux/36000. Vérifié : aucune ville hors-dept.
  2. Pour chaque ville, page liste paginée :
     /immobilier/vente/maison/{ville-slug}/{CP}[?page=N]
     → cartes d'annonces dont l'URL détail contient le CODE POSTAL :
       /immobilier/vente/maison/{ville}/{CP}/{N}pieces/{id}
  3. Post-filtre STRICT : code_postal[:2] == dept (le CP vient de l'URL détail).

Cartes : div.card.item-annonce
  - URL détail : a[href*="/{CP}/{N}pieces/{id}"]  → CP, pièces, id_annonce
  - Titre      : h2.card-title.title-annonce  → "Vente Maison à {Ville} {N} pièces | {S} m²"
  - Prix       : span.prix (ou p.prix)  → "86 700 €"
  - Photo      : img.image-list-annonce[src="/visuels/{base64}"] (proxy le-partenaire)
  - Nb photos  : div.text-photo-annonce
  - Description : texte de la carte (extrait)

Type de bien : on ne scrape que le segment /maison/ (maisons / propriétés / longères).
L'annuaire départemental cape à ~30 villes (les mieux pourvues) → couverture partielle
mais sans fuite. Surface/pièces lus dans le titre ; terrain & DPE non exposés en liste
(enrichis ensuite par gallery.py sur les survivants).

Migré sur scrapers/_base.py : boucle départements (`run_dept_api`, la structure
villes-en-parallèle est propre à ce site), slugs (DEFAULT_DEPT_SLUGS), client,
garde-fou CP, dédup inter-villes et filtres prix/surface (keep_bien). Les
helpers _parse_price (coupe « ou xxx €/mois ») et _parse_surface (m² sans
suffixe « hab ») restent locaux — sémantique différente du socle.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import (
    DEFAULT_DEPT_SLUGS,
    get_with_retry,
    keep_bien,
    run_dept_api,
    standalone_main,
)

BASE_URL = "https://www.le-partenaire.fr"
MAX_PAGES = 6          # pages par ville
MAX_CITIES = 30        # l'annuaire départemental n'en liste pas plus
CITY_CONCURRENCY = 6   # villes scrapées en parallèle (borne anti-surcharge)
PHOTOS_PER_CARD = 1    # une seule vignette en liste ; gallery.py complète ensuite

# URL détail d'une annonce : /immobilier/vente/maison/{ville}/{CP}/{N}pieces/{id}
_DETAIL_RE = re.compile(r"/immobilier/vente/maison/[a-z0-9-]+/(\d{5})/(\d+)pieces/(\d+)")
# Lien ville sur l'annuaire départemental : .../{ville-slug}/{CP}
_CITY_RE = re.compile(r"^/immobilier/vente/maison/[a-z0-9-]+/(\d{5})$")


async def search(criteres: dict) -> list[dict]:
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    async def _fetch_dept(client, dept, slug):
        return await _scrape_dept(client, dept, slug, prix_max, prix_min, surface_min)

    return await run_dept_api(
        source="le_partenaire",
        label="LePartenaire",
        fetch_dept=_fetch_dept,
        criteres=criteres,
        dept_slugs=DEFAULT_DEPT_SLUGS,
        dept_sleep=0.6,
        client_kwargs={"timeout": 25},
    )


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    slug: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    # 1. Annuaire départemental → URLs villes (toutes du département)
    r = await get_with_retry(client, f"{BASE_URL}/immobilier/vente/maison/{slug}")
    if r is None or r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    city_paths: list[str] = []
    seen_city: set[str] = set()
    for a in soup.find_all("a", href=True):
        m = _CITY_RE.match(a["href"])
        if not m:
            continue
        cp = m.group(1)
        if cp[:2] != dept:          # garde-fou : ville hors-dept ⇒ ignorée
            continue
        if a["href"] in seen_city:
            continue
        seen_city.add(a["href"])
        city_paths.append(a["href"])

    # Villes scrapées en parallèle (borne CITY_CONCURRENCY) — l'ancien parcours
    # séquentiel (~85 s/dept) saturait le batch parallèle du hunter.
    sem = asyncio.Semaphore(CITY_CONCURRENCY)

    async def _city(city_path: str) -> list[dict]:
        async with sem:
            return await _scrape_city(
                client, dept, city_path, prix_max, prix_min, surface_min,
            )

    city_lists = await asyncio.gather(
        *[_city(cp) for cp in city_paths[:MAX_CITIES]],
        return_exceptions=True,
    )

    # La dédup inter-villes est assurée par le keep_bien de run_dept_api.
    biens: list[dict] = []
    for lst in city_lists:
        if isinstance(lst, Exception) or not lst:
            continue
        biens.extend(lst)
    return biens


async def _scrape_city(
    client: httpx.AsyncClient,
    dept: str,
    city_path: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()        # dédup intra-ville (pagination)
    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}{city_path}" + (f"?page={page}" if page > 1 else "")
        r = await get_with_retry(client, url)
        if r is None or r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select("div.card.item-annonce")
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            try:
                bien = _parse_card(card, dept)
            except Exception:
                continue
            if not bien:
                continue
            # Filtre STRICT département (CP vient de l'URL détail) + dédup +
            # bornes prix/surface : keep_bien du socle
            if not keep_bien(bien, dept, seen_ids, prix_max=prix_max,
                             prix_min=prix_min, surface_min=surface_min):
                continue
            biens.append(bien)
            new_on_page += 1

        if new_on_page == 0:
            break

        await asyncio.sleep(0.4)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    link = card.find("a", href=_DETAIL_RE)
    if not link:
        return None
    href = link["href"]
    m = _DETAIL_RE.search(href)
    if not m:
        return None
    code_postal, pieces_str, id_annonce = m.group(1), m.group(2), m.group(3)
    url = href if href.startswith("http") else BASE_URL + href

    pieces = int(pieces_str) if pieces_str.isdigit() else None

    # Titre : "Vente Maison à Châtillon-Sur-Indre 5 pièces | 99 m²"
    title_el = card.select_one("h2.card-title, h2.title-annonce")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    titre = re.sub(r"\s+", " ", titre).strip()

    ville = _parse_ville(titre)
    surface = _parse_surface(titre)
    if pieces is None:
        m_p = re.search(r"(\d+)\s*pi[eè]ce", titre, re.IGNORECASE)
        if m_p:
            pieces = int(m_p.group(1))

    # Prix
    price_el = card.select_one("span.prix") or card.select_one("p.prix")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Description : texte de la carte hors titre/prix
    desc_el = card.select_one(".description-annonce, .text-annonce, p.card-text")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""
    if not description:
        # repli : tout le texte de la carte, nettoyé
        raw = card.get_text(" ", strip=True)
        description = re.sub(r"\s+", " ", raw)[:1200]

    # Photo (vignette ; URL proxy /visuels/ servie par le-partenaire)
    photos: list[str] = []
    img = card.select_one("img.image-list-annonce, img.card-img-top")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "le_partenaire",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Le Partenaire",
    }


# ── Helpers (sémantique propre au site — pas ceux du socle) ───────────────────

def _parse_ville(titre: str) -> str:
    """'Vente Maison à Châtillon-Sur-Indre 5 pièces | 99 m²' → 'Châtillon-Sur-Indre'"""
    m = re.search(r"\b[aà]\s+(.+?)\s+\d+\s*pi[eè]ce", titre, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # repli : entre 'à' et le séparateur '|'
    m = re.search(r"\b[aà]\s+(.+?)\s*\|", titre)
    if m:
        return m.group(1).strip()
    return ""


def _parse_surface(text: str) -> float | None:
    """Cherche 'NNN m²' (surface habitable affichée dans le titre)."""
    m = re.search(r"(\d[\d\s\xa0]*)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 2000:
                return f
        except ValueError:
            pass
    return None


def _parse_price(text: str) -> float | None:
    # n'extraire que le 1er montant (avant un éventuel "ou xxx €/mois")
    text = text.split("ou")[0]
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


if __name__ == "__main__":
    standalone_main(search, "Le Partenaire")

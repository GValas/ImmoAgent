"""scrapers/human_immobilier.py — Human Immobilier (~550 agences, absorbe La Bourse de l'Immobilier)

Méthode : scrape_simple (httpx) — SSR HTML avec querystring découverte via l'API interne.
Historique : blacklisté (human_immo) le 2026-05-26 « URL dept ignoré côté serveur ».
NOUVELLE stratégie (2026-07-04) : le POST /MoteurAccueil/RechercheBienByVille redirige
vers une URL GET porteuse du VRAI filtre serveur : le paramètre `ids={dept}` (id_polygone
renvoyé par le XHR /MoteurAccueil/GetVilles_Departements). Vérifié : ids=45 → 100 %
d'annonces 45xxx (0 fuite), filtres prix/surface serveur fonctionnels.

URL pattern : /achat-immobilier-{slug}?quartiers=&surface={smin}&surfaceMax=
              &prix={pmin}-{pmax}&typebien=1&nbpieces=&where={slug}&_b=1&_p=0
              &ids={dept}&page={N}         (typebien 1 = Maison ; page 1-based, 26/page)
Cartes : div.bien dans .container__biens — a.bien__link[title] « Vente Maison ORLEANS
         (45100) - 8 pièces - 198 m² », span.typeBien/.surface/.ville/.prix
         (les biens vendus affichent « Vendu » dans div.bien-vendu → écartés).
Couverture zone : depts 45/49/37/36/18/41 (aucune agence en 72/28/89/58/53 → 0 annonce).

PIÈGE session : le serveur mémorise la recherche dans le cookie de session
(AF_humanimmobiliercookie) et l'impose aux requêtes suivantes — une recherche sur un
dept sans stock (ex. 72) « empoisonne » toutes les suivantes (2 cartes suggestion).
D'où : un client httpx NEUF par département (pas de run_dept_search partagé).

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import (
    DEFAULT_DEPT_SLUGS,
    get_with_retry,
    keep_bien,
    make_client,
    parse_int,
    parse_price_digits,
    standalone_main,
)

BASE_URL = "https://www.human-immobilier.fr"
PHOTOS_PER_CARD = 10
MAX_PAGES = 8

_RE_CP = re.compile(r"\((\d{5})\)")
_RE_SURFACE = re.compile(r"(\d[\d\s\xa0]*)\s*m²", re.IGNORECASE)
_RE_ID = re.compile(r"_(\d+-\d+)$")


def _page_url(dept: str, slug: str, page: int, prix_min: int, prix_max: int,
              surface_min: int) -> str:
    return (
        f"{BASE_URL}/achat-immobilier-{slug}"
        f"?quartiers=&surface={surface_min or ''}&surfaceMax="
        f"&prix={prix_min or ''}-{prix_max or ''}"
        f"&typebien=1&nbpieces=&where={slug}&_b=1&_p=0"
        f"&ids={int(dept)}&page={page}"
    )


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_min = int(criteres.get("prix_min") or 0)
    prix_max = int(criteres.get("prix_max") or 0)
    surface_min = int(criteres.get("surface_min") or 0)
    results: list[dict] = []

    for dept in departements:
        slug = DEFAULT_DEPT_SLUGS.get(dept)
        if not slug:
            continue
        try:
            biens = await _scrape_dept(dept, slug, prix_min, prix_max, surface_min)
            results.extend(biens)
            print(f"[HumanImmobilier] Dept {dept}: {len(biens)} annonces")
        except Exception as e:
            print(f"[HumanImmobilier] Erreur dept {dept}: {e}")
        await asyncio.sleep(0.6)

    return results


async def _scrape_dept(dept: str, slug: str, prix_min: int, prix_max: int,
                       surface_min: int) -> list[dict]:
    """Un client httpx NEUF par département : le cookie de session mémorise la
    dernière recherche et écrase le querystring des requêtes suivantes."""
    biens: list[dict] = []
    seen_ids: set = set()
    pages_sans_nouveau = 0

    async with make_client() as client:
        for page in range(1, MAX_PAGES + 1):
            url = _page_url(dept, slug, page, prix_min, prix_max, surface_min)
            r = await get_with_retry(client, url)
            if r is None or r.status_code != 200:
                break
            cards = BeautifulSoup(r.text, "html.parser").select("div.bien")
            if not cards:
                break
            parsed = new = 0
            for card in cards:
                try:
                    bien = _parse_card(card, dept)
                except Exception:
                    continue
                if not bien:
                    continue
                parsed += 1
                if not keep_bien(bien, dept, seen_ids, prix_max=prix_max,
                                 prix_min=prix_min, surface_min=surface_min):
                    continue
                biens.append(bien)
                new += 1
            if parsed == 0:
                break
            if new == 0:
                pages_sans_nouveau += 1
                if pages_sans_nouveau >= 2:
                    break
            else:
                pages_sans_nouveau = 0
            await asyncio.sleep(0.5)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a.bien__link")
    if not link:
        return None
    href = link.get("href", "")
    if not href.startswith("/annonce-"):
        return None
    url = BASE_URL + href

    # Bien déjà vendu : bandeau « Vendu » à la place du prix
    if card.select_one(".bien-vendu"):
        return None

    titre = (link.get("title") or "").strip()
    m_cp = _RE_CP.search(titre)
    code_postal = m_cp.group(1) if m_cp else None

    ville_el = card.select_one("span.ville")
    ville = ville_el.get_text(strip=True) if ville_el else ""
    if not ville and titre:
        # « Vente Maison ORLEANS (45100) ... »
        m_v = re.search(r"Vente\s+\S+\s+(.+?)\s*\(\d{5}\)", titre)
        ville = m_v.group(1).strip() if m_v else ""

    type_el = card.select_one("span.typeBien")
    type_bien = (type_el.get_text(strip=True) if type_el else "maison").lower()

    prix_el = card.select_one("span.prix")
    prix = parse_price_digits(prix_el.get_text(strip=True) if prix_el else "")
    if not prix:
        return None  # pas de prix affiché (vendu / sous offre)

    surf_el = card.select_one("span.surface")
    surf_text = surf_el.get_text(" ", strip=True) if surf_el else ""
    surface = None
    m_s = _RE_SURFACE.search(surf_text or titre)
    if m_s:
        try:
            surface = float(re.sub(r"[\s\xa0]", "", m_s.group(1)))
        except ValueError:
            surface = None
    pieces = parse_int(r"(\d+)\s*pi[eè]ces?", surf_text or titre)

    m_id = _RE_ID.search(href)
    id_annonce = m_id.group(1) if m_id else href

    photos = []
    photo_el = card.select_one(".container-photo")
    if photo_el:
        for attr in ("data-src", "data-secondimg"):
            src = photo_el.get(attr) or ""
            if src.startswith("http"):
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "human_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150] or f"{type_bien.title()} {ville}".strip(),
        "type_bien": type_bien,
        "description": "",
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
        "agence": "Human Immobilier",
    }


if __name__ == "__main__":
    standalone_main(search, "Human Immobilier")

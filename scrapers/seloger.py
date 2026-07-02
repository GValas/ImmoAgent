"""
scrapers/seloger.py — SeLoger (portail majeur)
Méthode : scrape_simple (httpx) — URL legacy list.htm, SSR (page « explore »).

⚠️ Protection = DataDome (PAS Cloudflare). Situation RENVERSÉE le 2026-07-02 :
l'ancienne brèche (User-Agent de l'app iOS SeLoger) renvoie désormais 403, mais un
User-Agent navigateur DESKTOP repasse en 200 avec la page SSR complète (25 cartes,
mêmes sélecteurs `sl.explore.*`). Contraintes vérifiées ce jour :
  - la pagination (`LISTING-LISTpg=N`) est IGNORÉE : le serveur resert la page 1
    (l'UI « explore » pagine en XHR, endpoint non exposé) → 25 cartes max/requête ;
  - CONTOURNEMENT : recherche par département entier (`places=[{cp:NN}]` fonctionne)
    + filtres serveur `surface=min/NaN` et `price=lo/hi`, en BALAYANT des tranches
    de prix disjointes (vérifié : tranches → jeux d'annonces disjoints). Une tranche
    saturée (≥ ~25 cartes) est bissectée une fois pour récupérer le reste ;
  - réputation IP DataDome toujours collante → throttle entre requêtes, budget de
    requêtes par département, et ARRÊT du run entier au premier 403 (insister aggrave).
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import random
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import keep_bien, make_client
from scrapers._base import parse_float as _re_float
from scrapers._base import parse_int as _re_int

BASE_URL = "https://www.seloger.com"
SEARCH_URL = (
    "https://www.seloger.com/list.htm"
    "?types=2&natures=1&projects=2&enterprise=0"
    "&qsVersion=1.0&m=search_refine"
    "&places=[{{cp:{dept}}}]"
    "&surface={surface_min}/NaN"
    "&price={lo}/{hi}"
)

THROTTLE_S = 3.0          # délai moyen entre requêtes (réputation IP DataDome)
BAND_WIDTH = 100_000      # largeur initiale des tranches de prix (€)
BAND_MIN_WIDTH = 50_000   # en dessous, on ne bissecte plus
SATURATION = 24           # ≥ 24 cartes = tranche probablement tronquée → bissection
MAX_REQ_PER_DEPT = 10     # budget requêtes/département (throttle × budget ≈ 30 s/dept)

DEPARTEMENTS_CIBLES = ["72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"]


class _DataDomeBlocked(Exception):
    """L'IP est flaggée par DataDome (403) — inutile de continuer ce run."""


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = int(criteres.get("prix_max") or 600000)
    prix_min = int(criteres.get("prix_min") or 0)
    surface_min = int(criteres.get("surface_min") or 0)

    results: list[dict] = []
    seen_ids: set = set()
    async with make_client(timeout=25) as client:
        try:
            for dept in departements:
                kept = 0
                for bien in await _scrape_dept(client, dept, prix_min, prix_max, surface_min):
                    if keep_bien(bien, dept, seen_ids,
                                 prix_max=prix_max, prix_min=prix_min,
                                 surface_min=surface_min):
                        results.append(bien)
                        kept += 1
                print(f"[SeLoger] Dept {dept}: {kept} annonces")
        except _DataDomeBlocked:
            print(f"[SeLoger] DataDome a flaggé l'IP (403) — run interrompu "
                  f"({len(results)} annonces conservées avant blocage).")
    return results


def _initial_bands(prix_min: int, prix_max: int) -> list[tuple[int, int]]:
    """Découpe [prix_min, prix_max] en tranches de BAND_WIDTH (au moins une)."""
    lo = max(prix_min, 0)
    bands = []
    while lo < prix_max:
        hi = min(lo + BAND_WIDTH, prix_max)
        bands.append((lo, hi))
        lo = hi
    return bands or [(max(prix_min, 0), prix_max)]


async def _scrape_dept(
    client: httpx.AsyncClient, dept: str, prix_min: int, prix_max: int, surface_min: int
) -> list[dict]:
    """Balaye le département par tranches de prix disjointes (25 cartes SSR max par
    requête, pagination serveur inopérante). Une tranche saturée est bissectée
    (une passe), dans la limite du budget MAX_REQ_PER_DEPT."""
    out: list[dict] = []
    stack = list(reversed(_initial_bands(prix_min, prix_max)))   # LIFO, ordre croissant
    requests_done = 0

    while stack and requests_done < MAX_REQ_PER_DEPT:
        lo, hi = stack.pop()
        url = SEARCH_URL.format(dept=dept, surface_min=surface_min or "NaN", lo=lo or "NaN", hi=hi)
        try:
            r = await client.get(url)
        except httpx.HTTPError:
            await asyncio.sleep(THROTTLE_S)
            continue
        requests_done += 1
        if r.status_code in (403, 405) or "captcha-delivery" in r.text[:3000]:
            raise _DataDomeBlocked()
        if r.status_code == 200:
            cards = _parse_html(r.text, dept)
            out.extend(cards)
            # Tranche pleine → il y a probablement plus de 25 annonces dedans :
            # on la coupe en deux (les moitiés remplacent la tranche déjà lue —
            # les doublons sont dédupliqués par keep_bien via id_annonce).
            if len(cards) >= SATURATION and hi - lo > BAND_MIN_WIDTH:
                mid = (lo + hi) // 2
                stack.extend([(mid, hi), (lo, mid)])
        await asyncio.sleep(THROTTLE_S * random.uniform(0.7, 1.4))

    return out


def _parse_html(html: str, dept: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []

    cards = soup.select("[data-testid='sl.explore.card-container']")
    if not cards:
        cards = soup.select("[class*='cardMode']")

    for card in cards:
        try:
            bien = _parse_card(card, dept)
            if bien:
                results.append(bien)
        except Exception:
            continue
    return results


def _parse_card(card, dept: str) -> dict | None:
    # --- Prix ---
    price_el = card.select_one("[data-test='sl.price-label']")
    if not price_el:
        return None
    prix = _re_float(r"([\d\s\xa0]+)\s*€", price_el.get_text(strip=True).replace("\xa0", " "))
    if not prix:
        return None

    # --- Titre ---
    title_el = card.select_one("[data-test='sl.title']")
    titre = title_el.get_text(strip=True) if title_el else "Maison"

    # --- Tags : pièces, surface, chambres ---
    tags = [li.get_text(strip=True) for li in card.select("[data-test='sl.tagsLine'] li")]
    pieces = None
    surface = None
    chambres = None
    for tag in tags:
        if "pièce" in tag and pieces is None:
            pieces = _re_int(r"(\d+)", tag)
        elif "chambre" in tag and chambres is None:
            chambres = _re_int(r"(\d+)", tag)
        elif "m²" in tag and surface is None:
            surface = _re_float(r"([\d,\.]+)", tag.replace("\xa0", "").replace(" ", ""))

    # --- Adresse ---
    addr_el = card.select_one("[data-test='sl.address']")
    addr_text = addr_el.get_text(strip=True) if addr_el else ""
    cp_m = re.search(r"\((\d{5})\)", addr_text)
    cp = cp_m.group(1) if cp_m else ""
    ville = re.sub(r"à\s+", "", addr_text)
    ville = re.sub(r"\s*\(?\d{5}\)?\s*$", "", ville).strip()

    # --- Description (extrait carte) ---
    desc_el = card.select_one("[data-testid='sl.explore.card-description']")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # --- Lien ---
    link = card.select_one("a[href*='/annonces/achat/maison/']")
    if not link:
        link = card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    url = href if href.startswith("http") else BASE_URL + href

    id_m = re.search(r"/(\d{7,})", url)
    ad_id = id_m.group(1) if id_m else url[-20:]

    # --- Photos ---
    photos = []
    for img in card.select("[data-testid='sl.explore.PhotosContainer'] img[src]"):
        src = img.get("src", "")
        if src.startswith("http"):
            photos.append(src)

    return {
        "source": "seloger",
        "url": url,
        "id_annonce": ad_id,
        "titre": f"{titre} — {addr_text}"[:150],
        "type_bien": "maison",
        "description": description,
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:10],
        "dpe": None,
        "agence": "",
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "SeLoger")

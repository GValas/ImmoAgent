"""scrapers/europe_immobilier.py — Europe Immobilier (Avallon / Vézelay / Noyers /
L'Isle-sur-Serein, 89 ; déborde sur le nord 58 (Nevers, Clamecy, Pouilly-sur-Loire)
et quelques mandats épars 21/77/10/75 → post-filtre strict)

Méthode : scrape_simple (httpx) — SSR HTML, CMS La Boîte Immo « revolt »
(images europe-immobili.staticlbi.com, microdata schema.org/Product).
URL pattern : /maisons-villas-a-vendre/{page} (10 biens/page, ~180 biens tous
types au catalogue). ATTENTION : une URL inconnue re-sert la HOME en 200 (sans
carte) — l'arrêt se fait sur « 0 carte » ou « 0 carte nouvelle ».
Le POST /recherche/ pend (timeout serveur) → on ne l'utilise pas.

Cartes : li.panelBien (article itemscope Product)
  - URL/id  : a.btn-listing[href] "/1303-maison-8-pieces-...-annay-la-cote.html"
              (id numérique en tête de slug)
  - Prix    : span[itemprop=price][content="424000"]
  - Titre   : h1[itemprop=name] ("Maison 8 pièces...") ; extrait : h2
  - Type/surface/ville/CP : p (panel-footer) "Maison 198 m² - Avallon (89200)"
  - Réf     : span.ref[itemprop=productID] "Ref 2829vm"
Le terrain/DPE/description complète ne sont pas sur la carte → gallery.py.

Leçon vague 1 appliquée : pagination sur les CARTES BRUTES (dédup href), bornes
prix/surface appliquées ensuite — pas d'arrêt prématuré sur pages hors bornes.

NB infra : hébergement mutualisé La Boîte Immo (92.222.237.x, OVH) partagé avec
anjouimmobilier/hersantimmo/decizeimmo/agencetourangelle — l'infra temp-ban l'IP
en cas de rafale ; rester doux (sleep entre pages, une seule passe).

Gating : ne requête que si le 89 ou le 58 est demandé.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import (
    get_with_retry,
    keep_bien,
    make_client,
    parse_loc,
    standalone_main,
)

BASE_URL = "https://www.europeimmobilier.fr"
SOURCE = "europe_immobilier"
LABEL = "EuropeImmobilier"
AGENCE = "Europe Immobilier (Avallon)"
DEPTS_STOCK = {"89", "58"}
MAX_PAGES = 25
PHOTOS_PER_CARD = 5

_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|fermette|long[èe]re|manoir|ch[âa]teau|"
    r"moulin|demeure|domaine|g[îi]te|pavillon",
    re.IGNORECASE,
)


def _parse_card(card) -> dict | None:
    link = card.select_one("a.btn-listing") or card.select_one("a[href$='.html']")
    href = link.get("href", "") if link else ""
    if not href or not href.endswith(".html"):
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id numérique en tête du slug "/1303-maison-8-pieces-....html"
    m = re.match(r"^/?(\d+)-", href.lstrip("/"))
    id_annonce = m.group(1) if m else url

    price_el = card.select_one("span[itemprop=price]")
    prix = None
    if price_el:
        raw = price_el.get("content") or re.sub(r"[^\d]", "", price_el.get_text())
        try:
            prix = float(raw)
        except (TypeError, ValueError):
            prix = None

    name_el = card.select_one("[itemprop=name]")
    titre = name_el.get_text(" ", strip=True) if name_el else ""
    extrait_el = card.select_one(".bienTitle h2")
    extrait = extrait_el.get_text(" ", strip=True) if extrait_el else ""

    # Footer : "Maison\n 198 m² -\tAvallon (89200)"
    footer_el = card.select_one(".panel-footer p[itemprop=description]") or \
        card.select_one(".panel-footer p")
    footer = re.sub(r"\s+", " ", footer_el.get_text(" ", strip=True)) if footer_el else ""
    type_bien = footer.split()[0].lower() if footer else ""
    if not _KEEP_TYPE.search(type_bien or titre):
        return None
    surface = None
    m = re.search(r"([\d]+(?:[.,]\d+)?)\s*m²", footer)
    if m:
        surface = float(m.group(1).replace(",", "."))
    ville, code_postal = "", ""
    m = re.search(r"-\s*([^-]+\(\d{5}\))\s*$", footer)
    if m:
        ville, code_postal = parse_loc(m.group(1).strip())

    pieces = None
    m = re.search(r"(\d+)\s*pi[eè]ce", titre, re.IGNORECASE)
    if m:
        pieces = int(m.group(1))

    photos = []
    for img in card.select("img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:") and "logo" not in src.lower():
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": SOURCE,
        "url": url,
        "id_annonce": id_annonce,
        "titre": (titre or f"{type_bien.title()} {ville}").strip()[:150],
        "type_bien": type_bien or "maison",
        "description": extrait,
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": AGENCE,
    }


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    if not DEPTS_STOCK.intersection(departements):
        return []  # agence locale : rien à chercher hors de ses départements

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()
    seen_card_ids: set[str] = set()

    async with make_client() as client:
        for page in range(1, MAX_PAGES + 1):
            r = await get_with_retry(client, f"{BASE_URL}/maisons-villas-a-vendre/{page}")
            if r is None or r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select(".panelBien")
            if not cards:
                break  # fin de liste OU home re-servie (URL hors plage)

            # Fin de listing : stop si aucune carte nouvelle (le CMS peut
            # re-servir la dernière page au-delà de la fin).
            page_ids = []
            for card in cards:
                a = card.select_one("a.btn-listing") or card.select_one("a[href$='.html']")
                page_ids.append(a.get("href", "") if a else "")
            if page > 1 and all(cid in seen_card_ids for cid in page_ids):
                break
            seen_card_ids.update(page_ids)

            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue
                cp = bien["code_postal"]
                # Post-filtre STRICT département → 0 fuite hors zone (le stock
                # contient des mandats 21/77/10/75).
                if not cp or cp[:2] not in departements:
                    continue
                if not keep_bien(bien, cp[:2], seen_ids, prix_max=prix_max,
                                 prix_min=prix_min, surface_min=surface_min):
                    continue
                results.append(bien)

            await asyncio.sleep(0.6)

    par_dept: dict[str, int] = {}
    for b in results:
        par_dept[b["departement"]] = par_dept.get(b["departement"], 0) + 1
    for d, n in sorted(par_dept.items()):
        print(f"[{LABEL}] Dept {d}: {n} annonces")
    if not par_dept:
        print(f"[{LABEL}] 0 annonce retenue")

    return results


if __name__ == "__main__":
    standalone_main(search, AGENCE)

"""scrapers/lavisite.py — La Visite Immo (agence locale angevine, Maine-et-Loire 49)

Méthode : scrape_simple (httpx) — SSR HTML (thème WordPress "welko").

Listing : /nos-biens-achat-vente-location-gestion-locative/
  - filtre serveur : ?f_offre=1 (Achat) & f_type=2 (Maison)  → ~5 pages, ~8/page
  - pagination     : /page/{N}?f_offre=1&f_type=2
  - cartes         : a.card_estate
      · titre  : title attr / h3.title
      · type   : .card_estate--labels .label (Maison / Appartement)
      · offre  : .card_estate--labels .label (Achat / Location)
      · surface: .content--infos .text_icon  ("94,98 m²")
      · pièces : .content--infos .text_icon  ("5 Pièce(s)")
      · prix   : .content--price              ("254 100 €")
      · ville  : address                      ("Beaucouzé")  ← PAS de code postal
      · photo  : .card_estate--img img[src]
      · url    : href  /bien/{slug}/

FILTRE DÉPARTEMENT — pas de code postal ni de coords dans la liste NI dans la page
détail (le seul "49100 Angers" présent est l'adresse de l'agence en footer). On
RÉSOUT donc la ville → code département via l'API officielle geo.api.gouv.fr (BAN),
puis POST-FILTRE sur les départements cibles. C'est indispensable : l'agence vend
aussi hors-49 (ex. "Carquefou" = 44, "La Membrolle" = 37 apparaissent dans le stock).

Couverture : agence locale, gros du stock en 49 (Angers et couronne), quelques biens
limitrophes 44/37/53. Volume maisons à la vente faible (~35) mais réel.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.lavisite.immo"
LISTING_PATH = "/nos-biens-achat-vente-location-gestion-locative/"
MAX_PAGES = 12          # plafond de sécurité (~5 pages réelles pour maisons à la vente)
PHOTOS_PER_CARD = 1     # 1 photo de couverture dispo sur la liste

GEO_API = "https://geo.api.gouv.fr/communes"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# On ne garde que les maisons / propriétés (label "Maison" + heuristique titre).
_KEEP_TITLE = re.compile(
    r"maison|propri[ée]t[ée]|villa|long[èe]re|manoir|ch[âa]teau|demeure|"
    r"domaine|moulin|ferme|gentilhommi[èe]re|g[îi]te",
    re.IGNORECASE,
)
_EXCLUDE_TITLE = re.compile(
    r"appartement|studio|terrain|local|commerce|garage|parking|immeuble|bureau",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    raw: list[dict] = []
    seen_urls: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1) Récupère toutes les cartes "Achat / Maison" (filtre serveur)
        for page in range(1, MAX_PAGES + 1):
            path = LISTING_PATH if page == 1 else f"{LISTING_PATH}page/{page}"
            url = f"{BASE_URL}{path}?f_offre=1&f_type=2"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[LaVisite] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").find_all("a", class_="card_estate")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                bien = _parse_card(card)
                if not bien:
                    continue
                if bien["url"] in seen_urls:
                    continue
                seen_urls.add(bien["url"])
                raw.append(bien)
                new_on_page += 1

            if new_on_page == 0:
                break
            await asyncio.sleep(0.4)

        # 2) Résolution ville → département (geo.api.gouv.fr), avec cache
        geo_cache: dict[str, str] = {}
        for bien in raw:
            ville = bien.get("ville") or ""
            if not ville:
                bien["_dept"] = ""
                continue
            key = ville.lower().strip()
            if key not in geo_cache:
                geo_cache[key] = await _ville_to_dept(client, ville)
                await asyncio.sleep(0.15)
            bien["_dept"] = geo_cache[key]

    # 3) POST-FILTRE département + prix/surface
    results: list[dict] = []
    for bien in raw:
        dept = bien.pop("_dept", "")
        if departements and dept not in departements:
            continue
        bien["departement"] = dept
        bien["code_postal"] = ""   # non exposé par le site

        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        results.append(bien)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[LaVisite] Dept {dept}: {n} annonces")

    return results


def _parse_card(card) -> dict | None:
    href = card.get("href", "").strip()
    if not href or "/bien/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    labels = [p.get_text(strip=True) for p in card.select(".card_estate--labels .label")]
    labels_l = [x.lower() for x in labels]

    # Doit être une vente (Achat), pas une location
    if any("location" in x for x in labels_l):
        return None
    # Doit être une maison (label) — sinon on s'appuie sur le titre
    is_maison_label = any("maison" in x for x in labels_l)

    # Titre
    titre = (card.get("title") or "").strip()
    if not titre:
        h3 = card.select_one("h3.title, h3")
        titre = h3.get_text(" ", strip=True) if h3 else ""
    titre = re.sub(r"\s+", " ", titre).strip()

    # Filtre type via titre (sécurité)
    if _EXCLUDE_TITLE.search(titre):
        return None
    if not is_maison_label and not _KEEP_TITLE.search(titre):
        return None

    type_bien = "maison"

    # Ville (address) — sans code postal
    addr_el = card.select_one("address")
    ville = addr_el.get_text(" ", strip=True) if addr_el else ""
    ville = re.sub(r"\s+", " ", ville).strip()

    # Surface / pièces depuis .content--infos .text_icon
    surface = None
    pieces = None
    for ti in card.select(".content--infos .text_icon"):
        t = ti.get_text(" ", strip=True)
        if surface is None and re.search(r"m²", t):
            surface = _parse_num(t)
        m_p = re.search(r"(\d+)\s*Pi[èe]ce", t, re.IGNORECASE)
        if m_p:
            pieces = int(m_p.group(1))

    # Prix
    price_el = card.select_one(".content--price")
    prix = _parse_num(price_el.get_text(" ", strip=True)) if price_el else None

    # Photo de couverture
    photos = []
    img = card.select_one(".card_estate--img img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src.startswith("http") and not src.startswith("data:"):
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    # id_annonce depuis le slug
    slug = href.rstrip("/").split("/bien/")[-1]
    id_annonce = slug or url

    return {
        "source": "lavisite",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": None,
        "departement": "",      # rempli après géocodage
        "ville": ville[:80],
        "code_postal": "",
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "dpe": None,
        "photos": photos,
        "agence": "La Visite Immo",
    }


async def _ville_to_dept(client: httpx.AsyncClient, ville: str) -> str:
    """Résout un nom de commune → code département via l'API officielle BAN."""
    try:
        r = await client.get(
            GEO_API,
            params={
                "nom": ville,
                "fields": "codeDepartement",
                "boost": "population",
                "limit": 1,
            },
            headers={"Accept": "application/json"},
        )
        if r.status_code != 200:
            return ""
        data = r.json()
        if data and isinstance(data, list):
            return str(data[0].get("codeDepartement") or "")
    except Exception:
        pass
    return ""


def _parse_num(text: str) -> float | None:
    """'254 100 €' / '94,98 m²' → float."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ")
    cleaned = re.sub(r"[^\d,\. ]", "", cleaned).strip().replace(" ", "")
    cleaned = cleaned.replace(",", ".")
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


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
    print(f"\nTotal La Visite Immo (depts cibles): {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['ville']} ({b['type_bien']})"
        )

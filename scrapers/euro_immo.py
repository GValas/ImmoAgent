"""scrapers/euro_immo.py — Euro Immobilier Chalais (CMS Respacio, SSR)

Site : https://www.euro-immo.com — agence Sud-Ouest (Charente 16, Vienne 86,
       Deux-Sèvres 79, Charente-Maritime 17, Haute-Garonne 31, Ariège 09,
       Aude 11…). Spécialité : maisons de caractère / campagne, moulins, gîtes.

Méthode : scrape_simple (httpx) — SSR HTML pur (pas de Playwright).

URL pattern (liste globale, PAS de filtre département serveur) :
  - /biens-immobiliers/                  (page 1)
  - /biens-immobiliers/page/{N}/          (page N, ~28 cartes/page)
  → crawl global puis POST-FILTRE strict sur le département.

Cartes : div.respacio_card...card-grid-property
  - data-url            → /propriete/{id}/   (url détail + id_annonce)
  - span.icon-value     → type de bien ("Maison de Campagne", "Moulin", "Chateau"…)
  - .price-text .card-title → "299 000 € HAI"
  - <p>...</p>          → "Messé, Deux-Sèvres (79)"  (ville, département, code à 2 chiffres)
  - .propertycard_icons → 4 spans dans l'ordre : chambres, sdb, surface (179m²),
                          terrain (5193m²)
  - "Réf : 706794"      → référence (secours pour id_annonce)

Localisation : le code POSTAL n'est exposé nulle part (ni carte ni page détail —
les seuls nombres à 5 chiffres sont du CSS/JS). Le département est en revanche
explicite dans la carte : "Ville, Nom-Département (NN)". On post-filtre donc sur
ce **code département à 2 chiffres** (NN), pas sur code_postal[:2] — code_postal
reste vide (non disponible). 0 fuite garanti par le post-filtre dept strict.

Profil agence Sud-Ouest → 0 stock attendu dans la zone cible (72/28/45/89/49/
37/36/18/58/41/53) ; le scraper reste fonctionnel (0 fuite vérifié).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.euro-immo.com"
MAX_PAGES = 40
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien à conserver (maisons / propriétés de caractère). On exclut le
# locatif pur / commerce / terrain nu.
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|long[èe]re|manoir|ch[âa]teau|moulin|"
    r"demeure|domaine|mas|corps de ferme|g[îi]te|grange|prieur",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|viager|loft|box|cave|hangar",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = (
                f"{BASE_URL}/biens-immobiliers/"
                if page == 1
                else f"{BASE_URL}/biens-immobiliers/page/{page}/"
            )
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[EuroImmo] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select("div.respacio_card.card-grid-property")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                bien = _parse_card(card)
                if not bien:
                    continue
                # POST-FILTRE DÉPARTEMENT STRICT (code à 2 chiffres de la carte)
                if bien["departement"] not in departements:
                    continue
                if bien["id_annonce"] in seen_ids:
                    continue

                type_clean = bien.get("type_bien") or ""
                if _EXCLUDE_TYPE.search(type_clean) and not _KEEP_TYPE.search(type_clean):
                    continue
                if type_clean and not _KEEP_TYPE.search(type_clean):
                    continue

                p = bien.get("prix") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                s = bien.get("surface") or 0
                if surface_min and s and s < surface_min:
                    continue

                seen_ids.add(bien["id_annonce"])
                results.append(bien)
                new_on_page += 1

            # Pas de coupure sur new_on_page==0 : tout est hors-zone tant qu'on
            # n'a pas atteint la dernière page (post-filtre dept). On s'arrête
            # quand il n'y a plus de cartes (gérée plus haut).
            await asyncio.sleep(0.5)

    print(f"[EuroImmo] {len(results)} biens retenus dans la zone cible")
    return results


def _parse_card(card) -> dict | None:
    cdiv = card.select_one("div.card[data-url]")
    url = cdiv.get("data-url", "").strip() if cdiv else ""
    if not url:
        return None
    m_id = re.search(r"/propriete/(\d+)/", url)
    id_path = m_id.group(1) if m_id else ""

    type_el = card.select_one(".icon-value")
    type_bien = type_el.get_text(" ", strip=True) if type_el else ""

    price_el = card.select_one(".price-text .card-title")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Localisation : "Messé, Deux-Sèvres (79)"
    ville, dept = "", ""
    for p in card.select("p"):
        txt = p.get_text(" ", strip=True)
        m = re.search(r"^(.*?),\s*[\wÀ-ÿ'\- ]+\s*\((\d{2,3})\)\s*$", txt)
        if m:
            ville = m.group(1).strip()
            dept = m.group(2).strip()[:2]
            break

    # Référence (secours id_annonce)
    ref = ""
    m_ref = re.search(r"R[ée]f\s*:\s*(\w+)", card.get_text(" ", strip=True))
    if m_ref:
        ref = m_ref.group(1)
    id_annonce = id_path or ref or url
    if not dept or not id_annonce:
        return None

    # Icônes : chambres / sdb / surface(m²) / terrain(m²) dans l'ordre
    chambres = surface = surface_terrain = None
    icons = card.select(".propertycard_icons span")
    surfaces = []
    for sp in icons:
        t = sp.get_text(" ", strip=True)
        m_m2 = re.search(r"([\d\s\xa0]+)\s*m", t)
        if m_m2:
            surfaces.append(_to_float(m_m2.group(1)))
        else:
            m_n = re.search(r"(\d+)", t)
            if m_n and chambres is None:
                chambres = int(m_n.group(1))
    if surfaces:
        surface = surfaces[0]
        if len(surfaces) > 1:
            surface_terrain = surfaces[-1]

    titre = type_bien
    if ville:
        titre = f"{type_bien} {ville}".strip()

    # Description : liste d'attributs ("Pierre", "Piscine"…)
    attrs = [s.get_text(" ", strip=True) for s in card.select(".attributes span")]
    description = ", ".join(a for a in attrs if a)

    # Photo de carte (lazy)
    photos = []
    lazy = card.select_one(".make-lazy-url[data-lazy]")
    if lazy:
        src = lazy.get("data-lazy", "")
        if src.startswith("http"):
            photos.append(src)

    return {
        "source": "euro_immo",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": (type_bien or "maison")[:60],
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",  # non exposé par le site
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Euro Immobilier Chalais",
    }


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _to_float(s: str) -> float | None:
    val = re.sub(r"[\s\xa0]", "", s or "")
    try:
        return float(val) if val else None
    except ValueError:
        return None


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent))
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
    print(f"\nTotal EuroImmo: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:50]} — {b['prix']}€ — "
            f"{b.get('surface') or '?'}m² — terrain {b.get('surface_terrain') or '?'}m² "
            f"— {b['ville']}"
        )

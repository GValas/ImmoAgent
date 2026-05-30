"""scrapers/immobilierfranceouest.py — Immobilier France Ouest (agence Sarthe / vieilles pierres)

Méthode : scrape_simple (httpx) — SSR HTML (pas de JS nécessaire).

Couverture RÉELLE du site : agence régionale spécialisée "vieilles pierres"
(belles demeures, manoirs, longères, châteaux, propriétés de campagne).
Le menu de recherche n'expose QUE 5 départements + 2 régions :
    Sarthe (72), Maine-et-Loire (49), Mayenne (53), Loire-Atlantique (44),
    Morbihan (56)  +  régions Pays de Loire / Bretagne.
Sur les départements cibles du projet (72, 28, 45, 89, 49, 37, 36, 18, 58, 41, 53)
seuls 72, 49, 53 sont couverts. Les autres → 0 (le site n'a pas de stock).

Filtre département CÔTÉ SERVEUR via l'id GEO2020 du secteur :
    /recherche-achat/{slug}-(NN)_GEO2020-{geoid}.html
    ex : /recherche-achat/sarthe-(72)_GEO2020-3507.html
Le département apparaît aussi dans le slug des URL de fiche
    /achat-immobilier_{slug}-(NN)/{titre-slug}_{id}.html
→ on s'en sert comme double-vérification (0 fuite hors-dept).

Pagination : session-bound. La 1re requête sur l'URL GEO2020 arme une session
serveur (cookie) ; les pages suivantes se récupèrent via
    /recherche-achat_{NN}/nos-offres-page-{N}.html?p={N-1}
avec le MÊME client httpx (cookies conservés automatiquement).

Cartes : div[id^="item_"]
  - lien/titre : h4 a.text-extra-dark-gray  → "… - Ville (NN) à vendre"
  - id fiche   : dans l'URL  …_{id}.html
  - référence  : span.daha_sublink_listing
  - accroche   : p.listingAccroche (description)
  - caract.    : ul > li  "Pièces : 11", "Chambres : 8", "Détail : 268 m²"
  - prix       : h4.font-size18  "661 500 € HAI"
  - photo      : img.imgResp[src]

Volume : ~30-40 biens en Sarthe (72, 4 pages), quelques-uns en 49/53.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.immobilierfranceouest.com"
MAX_PAGES = 12
PHOTOS_PER_CARD = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Code département → id GEO2020 du secteur (relevé dans le menu de recherche).
# Le site ne couvre que ces secteurs ; les autres depts cibles n'ont pas de page.
DEPT_GEO2020: dict[str, str] = {
    "72": "3507",   # Sarthe
    "49": "3515",   # Maine-et-Loire
    "53": "3508",   # Mayenne
    "44": "3501",   # Loire-Atlantique (hors cible mais valide)
    "56": "3539",   # Morbihan (hors cible mais valide)
}

DEPT_SLUGS: dict[str, str] = {
    "72": "sarthe",
    "49": "maine-et-loire",
    "53": "mayenne",
    "44": "loire-atlantique",
    "56": "morbihan",
}

# Types à exclure (l'agence vend surtout des maisons/demeures, mais filtrons proprement)
_EXCLUDE_TYPE = re.compile(
    r"\bappartement\b|\bstudio\b|\bterrain\b|\bgarage\b|\bparking\b|"
    r"\blocal\b|\bcommerce\b|\bimmeuble\b|\bbureau\b|fonds de commerce",
    re.IGNORECASE,
)
_TYPE_MAP = [
    (re.compile(r"ch[âa]teau", re.IGNORECASE), "château"),
    (re.compile(r"manoir", re.IGNORECASE), "manoir"),
    (re.compile(r"moulin", re.IGNORECASE), "moulin"),
    (re.compile(r"long[èe]re", re.IGNORECASE), "longère"),
    (re.compile(r"propri[ée]t[ée]", re.IGNORECASE), "propriété"),
    (re.compile(r"demeure", re.IGNORECASE), "demeure"),
    (re.compile(r"villa", re.IGNORECASE), "villa"),
    (re.compile(r"maison", re.IGNORECASE), "maison"),
]


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            geoid = DEPT_GEO2020.get(dept)
            if not geoid:
                # Le site ne couvre pas ce département → rien à scraper.
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, geoid, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[ImmoFranceOuest] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[ImmoFranceOuest] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    geoid: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    slug = DEPT_SLUGS.get(dept, dept)
    # 1re requête : arme la session serveur (cookie) sur ce secteur.
    geo_url = f"{BASE_URL}/recherche-achat/{slug}-({dept})_GEO2020-{geoid}.html"
    r = await client.get(geo_url)
    if r.status_code != 200:
        return biens

    page = 1
    html = r.text
    while page <= MAX_PAGES:
        cards = BeautifulSoup(html, "html.parser").select('div[id^="item_"]')
        new_on_page = 0
        for card in cards:
            try:
                bien = _parse_card(card, dept)
            except Exception:
                continue
            if not bien:
                continue

            # Filtre dept (natif serveur) + double-vérif via le slug de fiche.
            if bien["departement"] != dept:
                continue

            aid = bien["id_annonce"]
            if aid in seen_ids:
                continue

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            seen_ids.add(aid)
            biens.append(bien)
            new_on_page += 1

        if new_on_page == 0 and page > 1:
            break

        # Page suivante (session-bound) : nos-offres-page-{page+1}.html?p={page}
        next_url = (
            f"{BASE_URL}/recherche-achat_{dept}/"
            f"nos-offres-page-{page + 1}.html?p={page}"
        )
        await asyncio.sleep(0.5)
        try:
            rn = await client.get(next_url)
        except Exception:
            break
        if rn.status_code != 200:
            break
        html = rn.text
        # plus aucune carte → fin
        if 'id="item_' not in html:
            break
        page += 1

    return biens


def _parse_card(card, dept: str) -> dict | None:
    # Lien + titre principal
    link = card.select_one("h4 a.text-extra-dark-gray") or card.select_one(
        'a[href*="/achat-immobilier_"]'
    )
    if not link or not link.get("href"):
        return None
    url = link["href"].strip()
    if not url.startswith("http"):
        url = BASE_URL + ("" if url.startswith("/") else "/") + url

    # Département depuis le slug de fiche : /achat-immobilier_{slug}-(NN)/...
    m_dept = re.search(r"achat-immobilier_[^/]*\((\d{2})\)/", url)
    dept_url = m_dept.group(1) if m_dept else dept

    # id de la fiche : ..._{id}.html
    m_id = re.search(r"_(\d+)\.html", url)
    id_num = m_id.group(1) if m_id else None

    titre = link.get_text(" ", strip=True)
    titre = re.sub(r"\s+à vendre\s*$", "", titre, flags=re.IGNORECASE).strip()

    if _EXCLUDE_TYPE.search(titre) and not re.search(
        r"maison|demeure|propri|manoir|ch[âa]teau|moulin|villa|long[èe]re", titre, re.I
    ):
        return None

    # Ville : segment avant "(NN)" dans le titre  ("… - La Flèche (72)")
    ville = ""
    m_v = re.search(r"[-–]\s*([^-–(]+?)\s*\(\d{2}\)\s*$", titre)
    if m_v:
        ville = m_v.group(1).strip()
    else:
        m_v2 = re.search(r"\b([A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-\s]+?)\s*\(\d{2}\)\s*$", titre)
        if m_v2:
            ville = m_v2.group(1).strip()

    # Référence
    ref_el = card.select_one(".daha_sublink_listing")
    ref = ref_el.get_text(strip=True) if ref_el else ""
    id_annonce = id_num or ref or url

    # Description / accroche
    accroche_el = card.select_one(".listingAccroche")
    description = accroche_el.get_text(" ", strip=True) if accroche_el else ""

    # Caractéristiques : ul > li  "Pièces : 11", "Chambres : 8", "Détail : 268 m²"
    pieces = chambres = None
    surface = None
    for li in card.select("ul li"):
        txt = li.get_text(" ", strip=True)
        low = txt.lower()
        if "pièce" in low or "piece" in low:
            m = re.search(r"(\d+)", txt)
            if m:
                pieces = int(m.group(1))
        elif "chambre" in low:
            m = re.search(r"(\d+)", txt)
            if m:
                chambres = int(m.group(1))
        elif "m" in low and re.search(r"\d", txt):
            # "Détail : 268 m²" (surface habitable)
            m = re.search(r"([\d\s\xa0]+)\s*m", txt)
            if m:
                val = re.sub(r"[\s\xa0]", "", m.group(1))
                if val.isdigit():
                    f = float(val)
                    if 8 <= f <= 3000:
                        surface = f

    # Prix : h4.font-size18 contenant "661 500 €"
    prix = None
    for h in card.select("h4.font-size18"):
        t = h.get_text(" ", strip=True)
        if "€" in t:
            prix = _parse_price(t)
            if prix:
                break

    # Type de bien
    type_bien = "maison"
    for rx, label in _TYPE_MAP:
        if rx.search(titre) or (description and rx.search(description[:200])):
            type_bien = label
            break

    # Photo de couverture
    photos = []
    img = card.select_one("img.imgResp") or card.select_one("img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src.startswith("http") and not src.startswith("data:"):
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "immobilierfranceouest",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept_url,
        "ville": ville[:80],
        "code_postal": "",  # non fourni sur la liste ; dept fiable via slug/GEO
        "surface": surface,
        "surface_terrain": _parse_terrain(description),
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Immobilier France Ouest",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    m = re.search(r"([\d\s\xa0\.]+)\s*€", text)
    if not m:
        return None
    cleaned = re.sub(r"[\s\xa0\.]", "", m.group(1))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_terrain(text: str) -> float | None:
    """'terrain de 7 000 m²' → 7000.0"""
    if not text:
        return None
    m = re.search(r"terrain[^0-9]{0,15}([\d\s\xa0]+)\s*m", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        if val.isdigit():
            f = float(val)
            if 10 <= f <= 5_000_000:
                return f
    return None


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
    print(f"\nTotal Immobilier France Ouest: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    # Contrôle de fuite
    cibles = {str(d).zfill(2) for d in criteres.departements}
    fuites = [b for b in biens if b["departement"] not in cibles]
    print(f"Fuites hors-dept cibles : {len(fuites)}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

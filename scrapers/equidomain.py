"""scrapers/equidomain.py — Equidomain (immobilier équestre : propriétés, haras, fermes équestres)

Méthode : scrape_simple (httpx) — SSR HTML (.htm), pas de JS requis.

URL pattern (listing par RÉGION) :
    /fr/annonces/equestre-a-vendre/propriete-equestre/{region}.htm[?page=N]
    Pagination : ?page=2, ?page=3 … (lien "Suivant" tant qu'il existe).

⚠️ Piège majeur du filtre département / région :
    Le site n'a PAS de filtre par département. Le filtre se fait par RÉGION dans
    l'URL, MAIS seuls les slugs des ANCIENNES régions sont reconnus :
      - "centre"          → 16 ann.  (Indre-et-Loire 37, Loiret 45, Loir-et-Cher 41,
                                       Cher 18, Eure-et-Loir 28, Indre 36)
      - "bourgogne"       → 12 ann.  (Yonne 89, Nièvre 58, Saône-et-Loire 71, Côte d'Or 21)
      - "pays-de-la-loire"→ 43 ann.  (Loire-Atlantique 44, Sarthe 72, Maine-et-Loire 49,
                                       Mayenne 53, Vendée 85)
    Un slug NON reconnu (ex "centre-val-de-loire") ne renvoie PAS d'erreur : il
    RETOMBE silencieusement sur l'inventaire NATIONAL complet (349 annonces, tous
    départements) → fuite massive. On n'utilise donc QUE les slugs validés ci-dessus.

    Sécurité supplémentaire : aucune annonce ne porte de code postal sur la liste,
    seulement le NOM du département dans td.cell_localisation. On mappe ce nom vers
    son code (NORM_DEPT) et on POST-FILTRE strictement sur les départements cibles.
    Double garde-fou ⇒ 0 fuite garantie même si un slug régional retombe sur national.

Cartes : table.myadlist.table_item (id="tab_{ref}")
  - URL/titre : td.cell_title a[href][title]   (href en //www… → https:)
  - prix      : .cell_price                     ("333 000 €")
  - localisation : td.cell_localisation         ("Indre-et-Loire Professionnel")
  - description  : .desc-line
  - détails   : td.cell_details li (span libellé / span.detail_info valeur)
                 → "Propriété :", "Box :", "Surface propriété :" (en HECTARES → terrain)
  - photo     : span.bzzzz[style] background url(...)

Limites :
  - Pas de surface HABITABLE ni de code postal sur la liste (seul le dept-nom).
    On remplit surface_terrain (ha→m²) ; surface (habitable) reste souvent None.
  - On garde uniquement les biens type maison/propriété (tout l'inventaire l'est).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import unicodedata

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.equidomain.com"
LISTING = BASE_URL + "/fr/annonces/equestre-a-vendre/propriete-equestre/{region}.htm"
MAX_PAGES = 8
PHOTOS_PER_CARD = 1


# Slugs de RÉGION (anciennes régions) reconnus par le site. Chaque slug couvre
# plusieurs départements cibles. NE PAS utiliser les slugs "nouvelles régions"
# (ex centre-val-de-loire) : ils retombent sur l'inventaire national.
REGION_SLUGS = ["centre", "bourgogne", "pays-de-la-loire"]

# Nom de département (td.cell_localisation, sans accents/casse normalisés) → code.
# Couvre les départements présents dans les 3 régions ci-dessus.
NORM_DEPT: dict[str, str] = {
    "indre-et-loire": "37",
    "loiret": "45",
    "loir-et-cher": "41",
    "cher": "18",
    "eure-et-loir": "28",
    "indre": "36",
    "yonne": "89",
    "nievre": "58",
    "saone-et-loire": "71",
    "cote d'or": "21",
    "cote-d'or": "21",
    "loire-atlantique": "44",
    "sarthe": "72",
    "maine-et-loire": "49",
    "mayenne": "53",
    "vendee": "85",
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0

    results: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for region in REGION_SLUGS:
            try:
                cards = await _fetch_region(client, region)
            except Exception as e:
                print(f"[Equidomain] Erreur région {region}: {e}")
                continue

            for card in cards:
                bien = _parse_card(card)
                if not bien:
                    continue

                # POST-FILTRE strict par département (nom → code).
                dept = bien.get("departement") or ""
                if departements and dept not in departements:
                    continue

                p = bien.get("prix") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue

                aid = bien.get("id_annonce") or bien.get("url")
                if aid in seen:
                    continue
                seen.add(aid)
                results.append(bien)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[Equidomain] Dept {dept}: {n} annonces")

    return results


async def _fetch_region(client: httpx.AsyncClient, region: str) -> list:
    cards: list = []
    for page in range(1, MAX_PAGES + 1):
        url = LISTING.format(region=region)
        if page > 1:
            url += f"?page={page}"
        r = await client.get(url)
        if r.status_code != 200:
            break

        soup = BeautifulSoup(r.text, "html.parser")
        page_cards = soup.select("table.myadlist.table_item")
        if not page_cards:
            break
        cards.extend(page_cards)

        # Page suivante ?
        if not soup.select_one(f"a[href*='page={page + 1}']"):
            break
        await asyncio.sleep(0.4)

    return cards


def _parse_card(card) -> dict | None:
    try:
        a = card.select_one("td.cell_title a[href]")
        if not a:
            return None
        href = a.get("href", "").strip()
        if not href:
            return None
        if href.startswith("//"):
            url = "https:" + href
        elif href.startswith("http"):
            url = href
        else:
            url = BASE_URL + href

        titre = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
        titre = re.sub(r"\s+", " ", titre)

        # id annonce : id="tab_{ref}" sinon depuis le slug -{ref}.htm
        ref = ""
        cid = card.get("id", "")
        m = re.search(r"tab_(\d+)", cid)
        if m:
            ref = m.group(1)
        if not ref:
            m2 = re.search(r"-(\d+)\.htm", href)
            ref = m2.group(1) if m2 else url

        # Localisation : "Indre-et-Loire Professionnel" → dept nom
        loc_el = card.select_one("td.cell_localisation")
        loc_txt = loc_el.get_text(" ", strip=True) if loc_el else ""
        dept_nom = re.sub(
            r"\s*(Professionnel|Particulier).*$", "", loc_txt, flags=re.IGNORECASE
        ).strip()
        dept = NORM_DEPT.get(_norm(dept_nom), "")

        # Prix
        price_el = card.select_one(".cell_price")
        prix = _parse_num(price_el.get_text(" ", strip=True)) if price_el else None

        # Description
        desc_el = card.select_one(".desc-line")
        description = desc_el.get_text(" ", strip=True) if desc_el else ""

        # Détails (libellé → valeur)
        info: dict[str, str] = {}
        for li in card.select("td.cell_details li"):
            spans = li.select("span")
            if len(spans) >= 2:
                k = spans[0].get_text(strip=True).rstrip(":").strip()
                info[_norm(k)] = spans[1].get_text(strip=True)

        type_bien = info.get("propriete", "Propriété équestre") or "Propriété équestre"

        # Box (≈ chambres équivalent, on ne le mappe pas sur pieces)
        # Surface propriété en HECTARES → surface_terrain (m²)
        surface_terrain = None
        surf_ha = info.get("surface propriete") or info.get("surface")
        if surf_ha:
            mha = re.search(r"([\d.,]+)\s*ha", surf_ha, re.IGNORECASE)
            if mha:
                try:
                    ha = float(mha.group(1).replace(",", "."))
                    # Garde-fou : certaines annonces ont une saisie erronée côté site
                    # (ex "300000 ha" pour 30 ha). On rejette les valeurs absurdes
                    # (> 10 000 ha) plutôt que de polluer le scoring.
                    surface_terrain = ha * 10000 if 0 < ha <= 10000 else None
                except ValueError:
                    surface_terrain = None

        # Photo de couverture (background-image)
        photos: list[str] = []
        ph = card.select_one(".bzzzz")
        if ph:
            mph = re.search(r"url\((.*?)\)", ph.get("style", ""))
            if mph:
                src = mph.group(1).strip("'\"")
                if src.startswith("//"):
                    src = "https:" + src
                if src.startswith("http"):
                    photos.append(src)
        photos = photos[:PHOTOS_PER_CARD]

        return {
            "source": "equidomain",
            "url": url,
            "id_annonce": ref,
            "titre": titre[:150],
            "type_bien": type_bien.lower(),
            "description": description[:1200],
            "departement": dept,
            "ville": dept_nom,          # pas de ville exacte sur la liste
            "code_postal": "",          # absent de la liste (seul le dept-nom)
            "surface": None,            # surface habitable absente de la liste
            "surface_terrain": surface_terrain,
            "pieces": None,
            "chambres": None,
            "prix": prix,
            "dpe": None,
            "photos": photos,
            "agence": "Equidomain",
        }
    except Exception:
        return None


def _parse_num(text: str) -> float | None:
    cleaned = re.sub(r"[^\d,\.]", "", text.replace("\xa0", " ").replace(" ", ""))
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
    print(f"\nTotal Equidomain (depts cibles): {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

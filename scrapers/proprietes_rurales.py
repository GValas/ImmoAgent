"""scrapers/proprietes_rurales.py — Propriétés Rurales (annonces SAFER / propriétés agricoles & rurales)

Méthode : scrape_simple (httpx) — SSR HTML.
Site agrégeant les annonces SAFER : exploitations agricoles, propriétés équestres,
domaines, fermes, longères avec terres. Localisation au niveau DÉPARTEMENT
uniquement (pas de commune / code postal dans la liste ni la fiche détail).

URL pattern (filtre dept, redirige en 200 vers /vente-propriete-agricole/...) :
    /propriete-a-vendre/{region-slug}/{dept-slug},{NN}
    ex: /propriete-a-vendre/pays.de.la.loire/sarthe,72
    ex: /propriete-a-vendre/centre-val.de.loire/loiret,45
Pas de pagination : une seule page de cartes par département (7–30 annonces).

ATTENTION FUITE DÉPARTEMENT : la page d'un département inclut parfois des biens
limitrophes (ex. dept 49 contient une carte région ,44 ; dept 28 contient ,?).
Le filtre serveur est LÂCHE. On POST-FILTRE STRICTEMENT sur le code département
porté par le lien région de CHAQUE carte (a.safer_region_link href = ...,{NN}).

Cartes : div.res_tbl (itemtype schema.org/Offer)
  - URL/titre : h2 > a[href]  → /immobilier/...-fr_VN{id}.htm
  - description : p[itemprop=description]
  - dept (source de vérité) : a.safer_region_link[href]  → ".../{slug},{NN}"
  - prix : div.res_tbl_value[content="300000"]  (0 = "Nous consulter")
  - terrain : b.safer_land_value  → "11 ha 05 a 76 ca" (ha/a/ca → m²)
  - image : a.res_tbl1[style=background-image:url(...)]

Surface HABITABLE : non exposée dans la liste → surface=None.
La superficie annoncée est du TERRAIN (parcellaire agricole) → surface_terrain.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.proprietes-rurales.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Code département → "region-slug/dept-slug" (segment d'URL /propriete-a-vendre/{...},{NN})
DEPT_SLUGS: dict[str, str] = {
    "72": "pays.de.la.loire/sarthe",
    "28": "centre-val.de.loire/eure-et-loir",
    "45": "centre-val.de.loire/loiret",
    "89": "bourgogne-franche-comte/yonne",
    "49": "pays.de.la.loire/maine-et-loire",
    "37": "centre-val.de.loire/indre-et-loire",
    "36": "centre-val.de.loire/indre",
    "18": "centre-val.de.loire/cher",
    "58": "bourgogne-franche-comte/nievre",
    "41": "centre-val.de.loire/loir-et-cher",
    "53": "pays.de.la.loire/mayenne",
}

# Type de bien déduit du titre / description (inventaire majoritairement agricole).
_TYPE_MAP = [
    (re.compile(r"château|chateau", re.IGNORECASE), "château"),
    (re.compile(r"manoir", re.IGNORECASE), "manoir"),
    (re.compile(r"longère|longere", re.IGNORECASE), "longère"),
    (re.compile(r"moulin", re.IGNORECASE), "moulin"),
    (re.compile(r"équestre|equestre|haras|écurie|ecurie", re.IGNORECASE), "propriété équestre"),
    (re.compile(r"ferme|exploitation|élevage|elevage|maraîch|maraich|horticole|agricole", re.IGNORECASE), "ferme / exploitation"),
    (re.compile(r"domaine", re.IGNORECASE), "domaine"),
    (re.compile(r"propriété|propriete|demeure", re.IGNORECASE), "propriété"),
    (re.compile(r"maison", re.IGNORECASE), "maison"),
]


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                continue
            try:
                biens = await _scrape_dept(client, dept, slug, prix_max, prix_min)
                results.extend(biens)
                print(f"[ProprietesRurales] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[ProprietesRurales] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    slug: str,
    prix_max: int,
    prix_min: int,
) -> list[dict]:
    url = f"{BASE_URL}/propriete-a-vendre/{slug},{dept}"
    r = await client.get(url)
    if r.status_code != 200:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    cards = soup.select("div.res_tbl")

    biens: list[dict] = []
    seen: set[str] = set()
    for card in cards:
        bien = _parse_card(card, dept)
        if not bien:
            continue

        # FILTRE DÉPARTEMENT STRICT : le code porté par le lien région de la carte
        # fait foi (le listing inclut parfois des biens limitrophes).
        if bien["departement"] != dept:
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
        biens.append(bien)

    return biens


def _parse_card(card, dept_req: str) -> dict | None:
    # Lien + titre
    a = card.select_one("h2 a[href]") or card.select_one("a.res_tbl1[href]")
    if not a or not a.get("href"):
        return None
    href = a["href"]
    url = href if href.startswith("http") else BASE_URL + href
    titre = a.get_text(" ", strip=True)

    # id annonce depuis ...-fr_VN{id}.htm
    m_id = re.search(r"fr_(VN\d+)\.htm", href)
    id_annonce = m_id.group(1) if m_id else url

    # Département (source de vérité) : a.safer_region_link href "...,{NN}"
    reg = card.select_one("a.safer_region_link")
    dept_card = dept_req
    ville = None
    if reg:
        m_d = re.search(r",(\d{2})\b", reg.get("href", ""))
        if m_d:
            dept_card = m_d.group(1)
        # "Propriété Maine-et-Loire" → nom du département (pas de commune dispo)
        rtxt = reg.get_text(" ", strip=True)
        ville = re.sub(r"^Propriété\s+", "", rtxt).strip() or None

    # Description
    desc_el = card.select_one("p[itemprop=description]")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Prix : div.res_tbl_value[content] (0 = "Nous consulter")
    prix = None
    val_el = card.select_one(".res_tbl_value")
    if val_el and val_el.get("content"):
        try:
            v = float(val_el["content"])
            prix = v if v > 0 else None
        except (ValueError, TypeError):
            prix = None

    # Terrain : "11 ha 05 a 76 ca" → m²
    surface_terrain = None
    land_el = card.select_one(".safer_land_value")
    if land_el:
        surface_terrain = _parse_land_m2(land_el.get_text(" ", strip=True))

    # Type de bien
    blob = f"{titre} {description}"
    type_bien = "propriété rurale"
    for rx, label in _TYPE_MAP:
        if rx.search(blob):
            type_bien = label
            break

    # Image de couverture (background-image:url(...))
    photos = []
    img_a = card.select_one("a.res_tbl1")
    if img_a and img_a.get("style"):
        m_img = re.search(r"url\(([^)]+)\)", img_a["style"])
        if m_img:
            src = m_img.group(1).strip("'\"")
            if src.startswith("http"):
                photos.append(src)

    if not titre:
        titre = f"{type_bien.title()} {ville or ''}".strip()

    return {
        "source": "proprietes_rurales",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept_card,
        "ville": ville,
        "code_postal": "",  # non exposé (localisation au niveau département)
        "surface": None,     # surface habitable non exposée dans la liste
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "dpe": None,
        "photos": photos,
        "agence": "Propriétés Rurales (SAFER)",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_land_m2(text: str) -> float | None:
    """'11 ha 05 a 76 ca' → m²  (1 ha = 10000 m², 1 a = 100 m², 1 ca = 1 m²)."""
    if not text:
        return None
    ha = re.search(r"(\d+)\s*ha", text)
    a = re.search(r"(\d+)\s*a\b", text)
    ca = re.search(r"(\d+)\s*ca", text)
    total = 0.0
    found = False
    if ha:
        total += int(ha.group(1)) * 10000
        found = True
    if a:
        total += int(a.group(1)) * 100
        found = True
    if ca:
        total += int(ca.group(1))
        found = True
    return total if found and total > 0 else None


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
    print(f"\nTotal Propriétés Rurales: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    # Contrôle de fuite : tout dept hors-cible est une fuite
    cibles = {str(d).zfill(2) for d in criteres.departements}
    fuites = [b for b in biens if b["departement"] not in cibles]
    print(f"FUITES hors-département : {len(fuites)}")
    for b in fuites[:5]:
        print(f"  FUITE [{b['departement']}] {b['titre'][:50]} — {b['url']}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']}"
        )

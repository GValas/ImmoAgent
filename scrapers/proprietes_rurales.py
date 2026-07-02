"""scrapers/proprietes_rurales.py — Propriétés Rurales (annonces SAFER / propriétés agricoles & rurales)

Méthode : scrape_simple (httpx) — SSR HTML.
Site agrégeant les annonces SAFER : exploitations agricoles, propriétés équestres,
domaines, fermes, longères avec terres. Localisation au niveau DÉPARTEMENT
uniquement (pas de commune / code postal dans la liste ni la fiche détail).

Migré sur scrapers/_base.py (modèle le_tuc.py) : boucle département, dédup et
filtres prix viennent du socle (run_dept_api — une seule page par département,
pas de pagination). Ne reste ici que le PROPRE au site : slugs region/dept,
post-filtre département par carte et parsing.

URL pattern (filtre dept, redirige en 200 vers /vente-propriete-agricole/...) :
    /propriete-a-vendre/{region-slug}/{dept-slug},{NN}
    ex: /propriete-a-vendre/pays.de.la.loire/sarthe,72
Pas de pagination : une seule page de cartes par département (7–30 annonces).

ATTENTION FUITE DÉPARTEMENT : la page d'un département inclut parfois des biens
limitrophes (ex. dept 49 contient une carte région ,44). Le filtre serveur est
LÂCHE. On POST-FILTRE STRICTEMENT sur le code département porté par le lien
région de CHAQUE carte (a.safer_region_link href = ...,{NN}).

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

import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, run_dept_api, standalone_main

BASE_URL = "https://www.proprietes-rurales.com"


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
    return await run_dept_api(
        source="proprietes_rurales",
        label="ProprietesRurales",
        fetch_dept=_fetch_dept,
        criteres=criteres,
        dept_slugs=DEPT_SLUGS,
        dept_sleep=0.6,
    )


async def _fetch_dept(client, dept: str, slug: str | None) -> list[dict]:
    r = await get_with_retry(client, f"{BASE_URL}/propriete-a-vendre/{slug},{dept}")
    if r is None or r.status_code != 200:
        return []

    biens: list[dict] = []
    for card in BeautifulSoup(r.text, "html.parser").select("div.res_tbl"):
        bien = _parse_card(card, dept)
        # FILTRE DÉPARTEMENT STRICT : le code porté par le lien région de la carte
        # fait foi (le listing inclut parfois des biens limitrophes).
        if bien and bien["departement"] == dept:
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


# ── Helpers propres au site ───────────────────────────────────────────────────

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


if __name__ == "__main__":
    standalone_main(search, "Propriétés Rurales")

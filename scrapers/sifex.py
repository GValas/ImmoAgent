"""scrapers/sifex.py — SIFEX Ltd (agence anglophone, propriétés de prestige en France)

Méthode : scrape_simple (httpx) — SSR HTML (site PHP « siteweb65 »).
URL liste : /french-property/sales/department/{NomDept}/{NN}
            → filtre département CÔTÉ SERVEUR via le slug nom+numéro
              (ex: /department/Indre-et-Loire/37, /department/Cher/18,
               /department/Indre/36). Vérifié : aucune fuite hors-dept.
Pagination : /…/{NN}/{offset}  (la liste tient souvent sur une page pour la zone).
Cartes : div.propbox
  - a[href*='/sales/.../{id}']  → URL détail (+ id numérique final)
  - img[alt]   → "Castle For Sale, TOURS, 37000, FRANCE" → ville + CODE POSTAL
  - h5 > b     → type ("Castle", "Manor House", "Country House", "Water Mill"…)
  - h4         → "Indre-et-Loire (37)"  (dept de contrôle)
  - "Ref:"     → référence (id_annonce)
  - "€ N,NNN,NNN"  → prix
  - "Bedrooms: N", "Land: N ha"  → chambres, surface terrain (ha→m²)
  - .prop_panel_desc → description (anglais)

Post-filtre dept STRICT sur le CODE POSTAL extrait de l'alt (CP[:2] ∈ depts).
Site orienté PRESTIGE (châteaux/manoirs Loire Valley) : beaucoup de biens
dépassent prix_max — ils sont écartés par le filtre prix du pipeline ; le scraper
remonte ceux dans la fourchette. Types non résidentiels (terrain/business)
écartés.

Interface : async def search(criteres: dict) -> list[dict]
"""
import re

from scrapers._base import parse_int, parse_price, run_dept_search, standalone_main

BASE_URL = "https://www.sifex.co.uk"

# Slug département SIFEX : "Nom-Complet" (avec tirets) + numéro.
DEPT_SLUGS = {
    "72": "Sarthe",
    "28": "Eure-et-Loir",
    "45": "Loiret",
    "89": "Yonne",
    "49": "Maine-et-Loire",
    "37": "Indre-et-Loire",
    "36": "Indre",
    "18": "Cher",
    "58": "Nievre",
    "41": "Loir-et-Cher",
    "53": "Mayenne",
}

_EXCLUDE_TYPE = re.compile(
    r"\b(building plot|land|commercial|business|apartment|office|shop|"
    r"garage|car park)\b",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    return await run_dept_search(
        source="sifex",
        label="SIFEX",
        page_url=lambda dept, slug, page: (
            f"{BASE_URL}/french-property/sales/department/{slug}/{dept}"
            + (f"/{(page - 1) * 12}" if page > 1 else "")
        ),
        card_selector="div.propbox",
        parse_card=_parse_card,
        criteres=criteres,
        dept_slugs=DEPT_SLUGS,
        max_pages=4,
    )


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a[href*='/sales/']")
    href = link.get("href", "") if link else ""
    if not href or not re.search(r"/sales/[a-z-]+/[a-z-]+/\d+", href):
        return None
    url = href if href.startswith("http") else BASE_URL + href

    img = card.select_one("img[alt]")
    alt = img.get("alt", "") if img else ""

    # type ("Castle", "Manor House"…)
    type_el = card.select_one("h5 b") or card.select_one("h5")
    type_bien = (type_el.get_text(" ", strip=True) if type_el else "").strip()
    type_src = type_bien + " " + alt
    if _EXCLUDE_TYPE.search(type_src):
        return None
    type_bien = type_bien.lower() or "maison"

    # ville + CP depuis l'alt : "Castle For Sale, TOURS, 37000, FRANCE"
    code_postal = ""
    ville = ""
    m = re.search(r",\s*([^,]+?),\s*(\d{5})\s*,\s*FRANCE", alt, re.IGNORECASE)
    if m:
        ville = m.group(1).strip().title()
        code_postal = m.group(2)
    if not code_postal:
        m2 = re.search(r"\b(\d{5})\b", alt)
        code_postal = m2.group(1) if m2 else ""

    full = card.get_text(" ", strip=True)

    # prix : "€ 2,955,000"
    prix = None
    mp = re.search(r"€\s*([\d,\s]+)", full)
    if mp:
        prix = parse_price(mp.group(1).replace(",", ""))

    # référence
    ref = ""
    mr = re.search(r"Ref\s*:?\s*([\w.-]+)", full, re.IGNORECASE)
    if mr:
        ref = mr.group(1)
    if not ref:
        mid = re.search(r"/(\d+)$", href)
        ref = mid.group(1) if mid else url
    id_annonce = ref

    chambres = parse_int(r"Bedrooms\s*:?\s*(\d+)", full)
    pieces = None

    # surface terrain : "Land: N ha" (ou m²)
    surface_terrain = None
    mha = re.search(r"Land\s*:?\s*([\d.,]+)\s*ha", full, re.IGNORECASE)
    if mha:
        try:
            surface_terrain = float(mha.group(1).replace(",", ".")) * 10000
        except ValueError:
            surface_terrain = None
    else:
        mm2 = re.search(r"Land\s*:?\s*([\d\s]+)\s*m²?", full, re.IGNORECASE)
        if mm2:
            try:
                surface_terrain = float(re.sub(r"\s", "", mm2.group(1)))
            except ValueError:
                surface_terrain = None

    desc_el = card.select_one(".prop_panel_desc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    titre = f"{type_bien.title()} {ville}".strip() or alt.split(",")[0]

    photos = []
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:") and "pictureloading" not in src:
            photos.append(src if src.startswith("http") else BASE_URL + src)

    return {
        "source": "sifex",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": None,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "SIFEX",
    }


if __name__ == "__main__":
    standalone_main(search, "SIFEX")

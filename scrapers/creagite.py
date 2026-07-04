"""scrapers/creagite.py — Creagîte (creagite.fr, gîtes & chambres d'hôtes à vendre)

Méthode : scrape_simple (httpx) — SSR HTML (site custom Bootstrap, aucun anti-bot).
Niche : établissements touristiques ruraux (gîtes, maisons/chambres d'hôtes) —
souvent de grandes demeures de caractère avec dépendances et terrain.
URL pattern : /etablissements-a-vendre/{region}/{dept-slug}[/page/{N}]
              → filtre département CÔTÉ SERVEUR par le chemin (0 fuite possible).
Cartes : div.border-top-line (h3 a = titre+lien ; p.id--NNNN = extrait + réf ;
p à glyphicon-briefcase = « {prix} € {type} à {Ville} ({Département}) »).
Pas de CP sur les cartes : le département étant verrouillé par l'URL, un CP
synthétique « NN000 » est posé (même convention que safer_annonces).
Certaines annonces sont des dépôts d'agences (ex. ImmoTourisme) : les doublons
inter-sources sont fusionnés en aval par la dédup du Hunter.

Interface : async def search(criteres: dict) -> list[dict]
"""
import re

from scrapers._base import (
    parse_price_digits,
    parse_surface,
    run_dept_search,
    standalone_main,
)

BASE_URL = "https://www.creagite.fr"

# Code département → segment {region}/{dept-slug} du site
DEPT_SLUGS = {
    "18": "centre/cher",
    "28": "centre/eure-et-loir",
    "36": "centre/indre",
    "37": "centre/indre-et-loire",
    "41": "centre/loir-et-cher",
    "45": "centre/loiret",
    "49": "pays-de-la-loire/maine-et-loire",
    "53": "pays-de-la-loire/mayenne",
    "58": "bourgogne/nievre",
    "72": "pays-de-la-loire/sarthe",
    "89": "bourgogne/yonne",
}

# Fonds de commerce purs exclus (on garde gîtes / maisons & chambres d'hôtes)
_EXCLUDE_TYPE = re.compile(r"camping|h[ôo]tel|restaurant|fonds", re.IGNORECASE)

# « 499 990 € gîte et chambres d'hôtes à Huisseau-sur-Cosson (Loir-et-Cher) »
_RE_PRIX_LOC = re.compile(r"^\s*(?:([\d][\d\s\xa0.,]*)€)?\s*(.+)$", re.DOTALL)


def _page_url(dept: str, slug: str, page: int) -> str:
    base = f"{BASE_URL}/etablissements-a-vendre/{slug}"
    return base if page == 1 else f"{base}/page/{page}"


async def search(criteres: dict) -> list[dict]:
    return await run_dept_search(
        source="creagite",
        label="Creagite",
        page_url=_page_url,
        card_selector="div.border-top-line",
        parse_card=_parse_card,
        criteres=criteres,
        dept_slugs=DEPT_SLUGS,
    )


def _parse_card(card, dept: str) -> dict | None:
    a = card.select_one("h3 a")
    if not a or not a.get("href"):
        return None
    href = a.get("href", "")
    url = href if href.startswith("http") else BASE_URL + href
    titre = a.get_text(" ", strip=True)

    # Référence : classe id--NNNN du paragraphe d'extrait
    id_annonce = url
    desc = ""
    p_id = card.select_one("p[class*=id--]")
    if p_id:
        desc = p_id.get_text(" ", strip=True)
        m = re.search(r"id--(\d+)", " ".join(p_id.get("class") or []))
        if m:
            id_annonce = m.group(1)

    # Prix + type + ville : paragraphe au pictogramme « briefcase »
    prix = None
    type_bien = "gîte"
    ville = ""
    picto = card.find("span", class_="glyphicon-briefcase")
    if picto and picto.parent:
        txt = picto.parent.get_text(" ", strip=True)
        m = _RE_PRIX_LOC.match(txt)
        if m:
            if m.group(1):
                prix = parse_price_digits(m.group(1))
            reste = m.group(2).strip()
            # « {type} à {Ville} (Département) » — dernier « à » = localisation
            reste = re.sub(r"\s*\([^)]*\)\s*$", "", reste)
            if " à " in reste:
                type_bien, ville = reste.rsplit(" à ", 1)
            else:
                type_bien = reste
    if _EXCLUDE_TYPE.search(type_bien):
        return None

    surface = parse_surface(titre) or parse_surface(desc)

    # Terrain : « N ha » ou « N m² de terrain » dans titre/extrait
    surface_terrain = None
    m = re.search(r"([\d,.]+)\s*(?:ha\b|hectares?)", f"{titre} {desc}", re.IGNORECASE)
    if m:
        try:
            surface_terrain = float(m.group(1).replace(",", ".")) * 10_000
        except ValueError:
            pass

    photos = []
    img = card.select_one("img")
    if img:
        src = img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)

    return {
        "source": "creagite",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien.strip().lower()[:60],
        "description": desc[:1200],
        "departement": dept,
        "ville": ville.strip()[:80],
        # Pas de CP sur les cartes ; dept verrouillé par l'URL → CP synthétique
        "code_postal": f"{dept}000",
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": None,
    }


if __name__ == "__main__":
    standalone_main(search, "Creagite")

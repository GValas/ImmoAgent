"""scrapers/immobilier_center.py — Immobilier Center (Argenton-sur-Creuse / Châteauroux, Indre 36)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress / thème immo).
URL pattern : /page/{N}/   (liste paginée, ~12 biens/page, ~50 au total)
Cartes : article.property-item

Particularité : agence MONO-DÉPARTEMENT (Indre 36 uniquement — secteurs Argenton,
Châteauroux, La Châtre, Le Blanc…). Les cartes n'exposent NI code postal NI
coordonnées (seulement un libellé de secteur, ex. « Argenton Ville »). Le filtre
département est donc assuré par construction : on ne renvoie des biens QUE si le 36
est demandé, en taguant departement="36". Garde-fou supplémentaire : tout libellé
de secteur correspondant à une commune hors-36 connue est rejeté (aucun observé au
dernier test → 0 fuite).

Structure d'une carte :
  figure                 → statut « À Vendre »
  ul (1er)               → pièces / chambres / « Habitable NNN m² » / « Terrain NNN m² »
  h6 > a                 → titre + URL détail (/property/{slug}/)
  h6 (2e)                → ville / secteur
  p                      → description
  ul (dernier)           → « Réf.NNNN » + « NNN NNN€ »

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from scrapers._base import get_with_retry, make_client, parse_price
from bs4 import BeautifulSoup

BASE_URL = "https://immobilier-center.fr"
SOURCE = "immobilier_center"
LABEL = "ImmobilierCenter"
AGENCE = "Immobilier Center"
DEPT = "36"           # agence mono-département (Indre)
MAX_PAGES = 12
PHOTOS_PER_CARD = 1   # carte = 1 vignette ; galerie complète enrichie en page détail

# Communes hors-Indre (36) que l'on pourrait croiser en limite de secteur : on les
# rejette pour garantir 0 fuite. (Aucune observée au dernier test.)
_HORS_36 = re.compile(
    r"\b(limoges|guéret|gueret|montmorillon|le\s*dorat|bellac|"
    r"haute[- ]vienne|creuse|vienne)\b",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    if DEPT not in departements:
        print(f"[{LABEL}] Dept 36 non demandé → 0 annonce")
        return []

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen: set[str] = set()

    async with make_client() as client:
        for page in range(1, MAX_PAGES + 1):
            r = await get_with_retry(client, f"{BASE_URL}/page/{page}/")
            if r is None or r.status_code != 200:
                break
            cards = BeautifulSoup(r.text, "html.parser").select("article.property-item")
            if not cards:
                break
            new = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue
                aid = bien["id_annonce"]
                if aid in seen:
                    continue
                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue
                seen.add(aid)
                results.append(bien)
                new += 1
            if new == 0:
                break
            await asyncio.sleep(0.5)

    print(f"[{LABEL}] Dept 36: {len(results)} annonces")
    return results


def _parse_card(card) -> dict | None:
    h6s = card.find_all("h6")
    if not h6s:
        return None

    link = h6s[0].find("a", href=True)
    if not link:
        link = card.find("a", href=re.compile(r"/property/"))
    if not link:
        return None
    href = link.get("href", "")
    url = href if href.startswith("http") else BASE_URL + href
    titre = h6s[0].get_text(" ", strip=True)

    # Libellé ville / secteur (2e h6)
    ville = ""
    for h in h6s[1:]:
        txt = h.get_text(" ", strip=True)
        if txt and "€" not in txt and "Réf" not in txt:
            ville = txt
            break
    if _HORS_36.search(ville) or _HORS_36.search(titre):
        return None  # commune hors-36 → on écarte (0 fuite)

    # Prix : h6.tebpropprice ; référence : div « Réf.NNNN »
    price_el = card.select_one("h6.tebpropprice")
    prix = parse_price(price_el.get_text(" ", strip=True)) if price_el else None
    m_ref = re.search(r"R[ée]f\.?\s*([0-9A-Za-z]+)", card.get_text(" ", strip=True))
    id_annonce = m_ref.group(1) if m_ref else url

    # Pièces / chambres / surface / terrain : li[title=...] du 1er ul
    def _li_num(title: str) -> int | None:
        li = card.find("li", attrs={"title": title})
        if not li:
            return None
        m = re.search(r"\d+", li.get_text(" ", strip=True))
        return int(m.group()) if m else None

    def _li_m2(title: str) -> float | None:
        li = card.find("li", attrs={"title": title})
        if not li:
            return None
        m = re.search(r"([\d\s\xa0]+)\s*m", li.get_text(" ", strip=True))
        if not m:
            return None
        try:
            return float(re.sub(r"[\s\xa0]", "", m.group(1)))
        except ValueError:
            return None

    pieces = _li_num("Nombre de pièces")
    chambres = _li_num("Nombre de chambres")
    surface = _li_m2("Surface habitable")
    surface_terrain = _li_m2("Surface terrain")

    desc_el = card.find("p")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    photos = []
    img = card.find("img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)

    return {
        "source": SOURCE,
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": description[:1200],
        "departement": DEPT,
        "ville": ville[:80],
        "code_postal": "",     # non exposé par le site (secteur seulement)
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": AGENCE,
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Immobilier Center")

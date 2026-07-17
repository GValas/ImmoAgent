"""scrapers/ligloo.py — Ligloo (agrégateur d'annonces immobilières)

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /annonce-immobiliere/maison--{dept-slug}.html (page 1)
              /annonce-immobiliere/maison--{dept-slug}--{N}.html (pages suivantes)
              → filtre type+département CÔTÉ SERVEUR mais FUITES voisines fréquentes
              (ex. « sarthe » remonte du 49/61, « indre » du 44/37) → post-filtre
              STRICT code_postal[:2] via keep_bien (CP présent dans l'URL détail).
Cartes : li.li-result — chaque carte embarque un JSON-LD product (prix, surface,
         CP, lat/lon, photo). Ville/pièces/type reconstruits depuis le NOM DE
         FICHIER de la photo (seul endroit où ils figurent, les champs texte du
         JSON-LD sont vides). Agrégateur (crédite lesiteimmo & co) → doublons
         possibles, gérés par la dédup hunter (backup utile).
Pagination JS ctrl.set_page(N) côté client, mais la route SSR --{N} pagine bien
(recouvrement quasi nul vérifié entre pages).

Interface : async def search(criteres: dict) -> list[dict]
"""
import json
import re

from scrapers._base import run_dept_search, standalone_main

BASE_URL = "https://www.ligloo.com"

# L'URL détail (ligloo.fr) porte le CP : /annonce-immobiliere-detail/---{CP}/{ID}.html
_DETAIL_RE = re.compile(r"annonce-immobiliere-detail/---(\d{5})/(\w+)\.html")
# Nom de fichier photo : ...maison-{ville-slug}-{CP}-vente-maison-{N}-pieces-{S}-m...
_IMG_VILLE_RE = re.compile(r"(?:vente-|location-)?maison-([a-z0-9-]+?)-(\d{5})")
_IMG_PIECES_RE = re.compile(r"(\d+)-pieces")


def _page_url(dept: str, slug: str, page: int) -> str:
    suffix = "" if page == 1 else f"--{page}"
    return f"{BASE_URL}/annonce-immobiliere/maison--{slug}{suffix}.html"


async def search(criteres: dict) -> list[dict]:
    return await run_dept_search(
        source="ligloo",
        label="Ligloo",
        page_url=_page_url,
        card_selector="li.li-result",
        parse_card=_parse_card,
        criteres=criteres,
    )


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one('a[href*="annonce-immobiliere-detail"]')
    href = link.get("href", "") if link else ""
    m = _DETAIL_RE.search(href)
    if not m:
        return None
    code_postal, id_annonce = m.group(1), m.group(2)
    url = href if href.startswith("http") else "https:" + href

    # JSON-LD product embarqué dans la carte (prix, surface, photo, coords)
    prix = surface = latitude = longitude = None
    photo = ""
    ld = card.find("script", type="application/ld+json")
    if ld:
        try:
            data = json.loads(ld.string or "{}")
            offers = data.get("offers") or {}
            try:
                prix = float(offers.get("price") or 0) or None
            except (TypeError, ValueError):
                prix = None
            cat = offers.get("category") or {}
            fs = (cat.get("floorSize") or {}).get("value")
            try:
                surface = float(fs) if fs else None
            except (TypeError, ValueError):
                surface = None
            try:
                latitude = float(cat.get("latitude"))
                longitude = float(cat.get("longitude"))
            except (TypeError, ValueError):
                latitude = longitude = None
            photo = data.get("image") or ""
        except Exception:
            pass

    fname = photo.rsplit("/", 1)[-1].lower() if photo else ""
    if "location" in fname:            # agrégateur mixte : on écarte le locatif
        return None

    ville = ""
    mv = _IMG_VILLE_RE.search(fname)
    if mv and mv.group(2) == code_postal:
        ville = mv.group(1).replace("-", " ").title()
    pieces = None
    mp = _IMG_PIECES_RE.search(fname)
    if mp:
        pieces = int(mp.group(1))

    # Portail source crédité sur la carte (lesiteimmo.com…) — trace utile pour la dédup
    src_el = card.select_one(".link-f-mysite")
    agence = f"via {src_el.get_text(strip=True)}" if src_el else None

    titre = "Maison"
    if pieces:
        titre += f" {pieces} pièces"
    if surface:
        titre += f" {surface:.0f} m²"
    if ville:
        titre += f" à {ville}"
    titre += f" ({code_postal})"

    bien = {
        "source": "ligloo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre,
        "type_bien": "maison",
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": [photo] if photo else [],
        "dpe": None,
        "agence": agence,
    }
    if latitude and longitude:
        bien["latitude"] = latitude
        bien["longitude"] = longitude
    return bien


if __name__ == "__main__":
    standalone_main(search, "Ligloo")

"""
scrapers/belles_demeures.py — Belles Demeures (SeLoger group, prestige)
Méthode : scrape_simple (httpx) — SSR complet sous UA desktop (réactivé 2026-07-02).

L'ancienne piste « bellesdemeures.com 403 sur pages filtrées » n'est plus vraie :
comme pour seloger.py, un UA navigateur desktop passe en 200. Recette vérifiée :
  - endpoint de recherche : /recherche?idtt=2&idtypebien=2&pl={place_id}
    avec filtres SERVEUR pxmin/pxmax/surfacemin (clés lues dans /bundles/search/js)
    et pagination &page=N (20 cartes SSR/page, max affiché dans .js_maxValue) ;
  - place_id par département (pl-272=18 … pl-332=72), relevés sur les pages région ;
  - le filtre départemental serveur est fiable (vérifié : 20/20 villes du dept),
    PAS de recherche élargie, mais les cartes n'exposent AUCUN code postal →
    code_postal="" et departement = dept de la requête (gallery/geoloc enrichiront).
Cartes : div.js_favoritesParent[id] avec div.type / div.specs / div.location /
div.price / div.desc / div.agency ; photos v.seloger.com dans le carrousel.
Interface : async def search(criteres: dict) -> list[dict]
"""
from scrapers._base import parse_float, parse_int, run_dept_search

BASE = "https://www.bellesdemeures.com"

# Place IDs Belles Demeures par département cible (relevés sur les pages région).
_PLACE_IDS = {
    "18": 272, "28": 273, "36": 274, "37": 275, "41": 276, "45": 277,
    "58": 278, "89": 280, "49": 330, "53": 331, "72": 332,
}


def _parse_card(card, dept: str) -> dict | None:
    ad_id = card.get("id", "")
    if not ad_id.isdigit():
        return None

    link = card.select_one("a.details[href]") or card.select_one("a.linkMask[href]")
    if not link:
        return None
    url = link["href"].split("?")[0].split("#")[0]
    if not url.startswith("http"):
        url = BASE + url

    price_el = card.select_one("div.price")
    prix = parse_float(r"([\d\s\xa0]{4,})\s*€",
                       (price_el.get_text(" ", strip=True) if price_el else "").replace("\xa0", " "))
    if not prix or prix < 10_000:
        return None

    specs = card.select_one("div.specs")
    specs_txt = specs.get_text(" ", strip=True).replace("\xa0", " ") if specs else ""
    pieces = parse_int(r"(\d+)\s*pièces?", specs_txt)
    chambres = parse_int(r"(\d+)\s*chambres?", specs_txt)
    surface = parse_float(r"([\d\s]+(?:[.,]\d+)?)\s*m²", specs_txt)

    loc_el = card.select_one("div.location")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville = loc.split(",")[-1].strip()[:80]     # « Quartier, Ville » → Ville

    type_el = card.select_one("div.type")
    type_txt = (type_el.get_text(strip=True) if type_el else "Maison").lower()
    type_bien = "chateau" if "château" in type_txt else "maison"

    desc_el = card.select_one("div.desc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""
    agence_el = card.select_one("div.agency")
    agence = agence_el.get_text(strip=True) if agence_el else "Belles Demeures"

    titre = (link.get("title") or "").strip() \
        or f"{type_el.get_text(strip=True) if type_el else 'Maison'} {specs_txt} à {ville}"

    photos = []
    for img in card.select("img[src], source[srcset]"):
        src = (img.get("src") or img.get("srcset") or "").split(",")[0].split(" ")[0]
        if src.startswith("http") and "visuels" in src and src not in photos:
            photos.append(src)

    return {
        "source": "belles_demeures",
        "url": url,
        "id_annonce": ad_id,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,        # filtre serveur pl-{id} fiable ; pas de CP en carte
        "ville": ville,
        "code_postal": "",
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:10],
        "dpe": None,
        "agence": agence,
    }


async def search(criteres: dict) -> list[dict]:
    prix_max = int(criteres.get("prix_max") or 0)
    prix_min = int(criteres.get("prix_min") or 0)
    surface_min = int(criteres.get("surface_min") or 0)

    def page_url(dept: str, pl: str, page: int) -> str:
        url = f"{BASE}/recherche?idtt=2&idtypebien=2&pl={pl}"
        if prix_min:
            url += f"&pxmin={prix_min}"
        if prix_max:
            url += f"&pxmax={prix_max}"
        if surface_min:
            url += f"&surfacemin={surface_min}"
        if page > 1:
            url += f"&page={page}"
        return url

    return await run_dept_search(
        source="belles_demeures",
        page_url=page_url,
        card_selector="div.js_favoritesParent[id]",
        parse_card=_parse_card,
        criteres=criteres,
        dept_slugs={d: str(pl) for d, pl in _PLACE_IDS.items()},
        max_pages=6,
        page_sleep=2.5,
        dept_sleep=3.0,
        label="BellesDemeures",
    )


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "BellesDemeures")

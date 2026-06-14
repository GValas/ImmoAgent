"""scrapers/etude_lodel.py — Étude Lodel / Viager Lodel (viager national)

Méthode : scrape_simple (httpx) — SSR HTML (moteur netty.immo)
URL : https://www.etudelodel.com/nos-biens-viager  (catalogue national, page unique)
       → pas de filtre département côté serveur : POST-FILTRE strict sur le code
         département `(NN)` présent dans le titre/description de chaque carte.
Cartes : div.res_div1 (microdata schema.org/Offer)
Particularités :
  - Site de VIAGER : le prix affiché (div.res_tbl_value[itemprop=price]) est le
    BOUQUET, pas une valeur de marché. On le remplit dans `prix` comme les autres
    scrapers viager du parc (costes_viager, viagimmo…).
  - Le code postal n'est pas structuré : seul le code département à 2 chiffres
    `(NN)` apparaît (titre/description) → on l'utilise pour le filtre dept et on
    fabrique un pseudo code_postal `NN000` pour le garde-fou du pipeline.
  - L'`itemprop=postalCode` éventuel pointe l'adresse de l'AGENCE (Paris) → ignoré.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_price, parse_surface

BASE_URL = "https://www.etudelodel.com"
LISTE_URL = f"{BASE_URL}/nos-biens-viager"


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)
    results: list[dict] = []
    seen: set[str] = set()

    async with make_client() as client:
        r = await get_with_retry(client, LISTE_URL)
        if r is None or r.status_code != 200:
            print(f"[Lodel] Listing inaccessible ({r.status_code if r else 'None'})")
            return results
        cards = BeautifulSoup(r.text, "html.parser").select("div.res_div1")
        print(f"[Lodel] {len(cards)} cartes dans le catalogue national")
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue
            dept = bien["departement"]
            if dept not in departements:
                continue
            if bien["id_annonce"] in seen:
                continue
            seen.add(bien["id_annonce"])
            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            # NB viager : le bouquet est bas → ne pas appliquer prix_min/max
            #             pour ne pas tout exclure (cohérent avec les autres
            #             scrapers viager). On applique seulement surface_min.
            if surface_min and s and s < surface_min:
                continue
            results.append(bien)
        await asyncio.sleep(0.4)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print(f"[Lodel] {len(results)} biens (zone) — par dept: {by_dept}")
    return results


_DEPT_RE = re.compile(r"\((\d{2})\)")


def _parse_card(card) -> dict | None:
    link = card.select_one("h2 a[href]") or card.select_one("a.res_tbl1[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    id_m = re.search(r"_([A-Za-z0-9]+)\.htm", href)
    id_annonce = id_m.group(1) if id_m else url

    title_el = card.select_one("h2")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    desc_el = card.select_one("p[itemprop=description]")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Code département : (NN) dans titre puis description
    m = _DEPT_RE.search(titre) or _DEPT_RE.search(description)
    if not m:
        return None
    dept = m.group(1)

    loc_el = card.select_one("div.loc_details")
    loc_text = loc_el.get_text(" ", strip=True) if loc_el else ""
    # 'Appartement Le Bourget-du-Lac 42.28 m²' → ville = sans le type ni la surface
    ville = re.sub(r"\d[\d.,]*\s*m².*$", "", loc_text).strip()
    ville = re.sub(r"^(Appartement|Maison|Villa|Propri[ée]t[ée])\s+", "", ville,
                   flags=re.IGNORECASE).strip()

    type_bien = "maison" if re.search(r"maison|villa|propri", loc_text + titre,
                                      re.IGNORECASE) else "appartement"

    price_el = card.select_one("div.res_tbl_value[itemprop=price]")
    prix = None
    if price_el:
        # le texte contient bouquet + rente ; isole le 1er montant (bouquet)
        txt = price_el.get_text(" ", strip=True)
        first = txt.split("+")[0]
        prix = parse_price(first)

    surface = None
    nobr = card.select_one("span.nobr")
    if nobr:
        surface = parse_surface(nobr.get_text(strip=True) + " m² hab")
        if surface is None:
            sm = re.search(r"([\d.,]+)\s*m²", nobr.get_text())
            if sm:
                try:
                    surface = float(sm.group(1).replace(",", "."))
                except ValueError:
                    surface = None

    photos = []
    a_img = card.select_one("a.res_tbl1[style*=background-image]")
    if a_img:
        murl = re.search(r"url\(([^)]+)\)", a_img.get("style", ""))
        if murl:
            photos.append(murl.group(1).strip("'\""))

    return {
        "source": "etude_lodel",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": f"{dept}000",  # pseudo-CP (seul le dept est connu)
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,  # = bouquet (viager)
        "photos": photos,
        "dpe": None,
        "agence": "Étude Lodel (viager)",
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Étude Lodel")

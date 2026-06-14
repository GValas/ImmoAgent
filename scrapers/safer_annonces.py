"""scrapers/safer_annonces.py — SAFER (propriétés agricoles / rurales) régionaux

Méthode : scrape_simple (httpx) — SSR HTML (moteur netty.immo, identique à etude_lodel)
Sites : sous-domaines régionaux de annonces-safer.fr
  - pays-de-la-loire.annonces-safer.fr  → depts 49, 53, 72
  - bourgogne-franche-comte.annonces-safer.fr → depts 58, 89
URL : /vente-propriete-agricole/{region-slug}/{dept-slug},{NN}  → filtre dept CÔTÉ
       SERVEUR (le code dept est dans l'URL) ; pagination ?page={N}.
Cartes : div.res_div1 (microdata schema.org/Offer)
Particularités :
  - Biens RURAUX / agricoles (exploitations, propriétés, terres + bâti) → terrain
    en `ha a ca` (b.safer_land_value) converti en m² (surface_terrain).
  - Prix fiable via l'attribut `content` de div.res_tbl_value[itemprop=price].
  - Pas de code postal sur la carte : seul le département (URL) est connu → pseudo
    code_postal `NN000` pour le garde-fou ; dept verrouillé par la boucle.
  - SAFER Centre (18/36/37/41/45/28) : sous-domaine non résolu au sondage → non
    inclus ici ; à ajouter si le sous-domaine est confirmé.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_price

MAX_PAGES = 8

# département → (sous-domaine, region-slug, dept-slug)
DEPT_CONF: dict[str, tuple[str, str, str]] = {
    "49": ("pays-de-la-loire", "pays-de-la-loire", "maine-et-loire"),
    "53": ("pays-de-la-loire", "pays-de-la-loire", "mayenne"),
    "72": ("pays-de-la-loire", "pays-de-la-loire", "sarthe"),
    "58": ("bourgogne-franche-comte", "bourgogne", "nievre"),
    "89": ("bourgogne-franche-comte", "bourgogne", "yonne"),
}


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    surface_min = criteres.get("surface_min", 0)
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    results: list[dict] = []

    async with make_client() as client:
        for dept in departements:
            conf = DEPT_CONF.get(dept)
            if not conf:
                continue
            sub, region, dept_slug = conf
            base = f"https://{sub}.annonces-safer.fr"
            seen: set[str] = set()
            count = 0
            for page in range(1, MAX_PAGES + 1):
                url = (f"{base}/vente-propriete-agricole/{region}/"
                       f"{dept_slug},{dept}?page={page}")
                r = await get_with_retry(client, url)
                if r is None or r.status_code != 200:
                    break
                cards = BeautifulSoup(r.text, "html.parser").select("div.res_div1")
                if not cards:
                    break
                new = 0
                for card in cards:
                    try:
                        bien = _parse_card(card, dept, base)
                    except Exception:
                        continue
                    if not bien or bien["id_annonce"] in seen:
                        continue
                    seen.add(bien["id_annonce"])
                    s = bien.get("surface") or 0
                    p = bien.get("prix") or 0
                    if surface_min and s and s < surface_min:
                        continue
                    if prix_max and p and p > prix_max:
                        continue
                    if prix_min and p and p < prix_min:
                        continue
                    results.append(bien)
                    new += 1
                count += new
                if new == 0:
                    break
                await asyncio.sleep(0.5)
            print(f"[SAFER] Dept {dept}: {count} annonces")
            await asyncio.sleep(0.6)

    return results


def _ha_a_ca_to_m2(text: str) -> float | None:
    """'04 ha 35 a 75 ca' → m² (1 ha=10000, 1 a=100, 1 ca=1)."""
    ha = re.search(r"(\d+)\s*ha", text)
    a = re.search(r"(\d+)\s*a\b", text)
    ca = re.search(r"(\d+)\s*ca", text)
    if not (ha or a or ca):
        return None
    total = 0.0
    if ha:
        total += int(ha.group(1)) * 10000
    if a:
        total += int(a.group(1)) * 100
    if ca:
        total += int(ca.group(1))
    return total or None


def _parse_card(card, dept: str, base: str) -> dict | None:
    link = card.select_one("h2 a[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else base + href

    id_m = re.search(r"_([A-Za-z0-9]+)\.htm", href)
    id_annonce = id_m.group(1) if id_m else url

    titre = link.get_text(" ", strip=True)
    desc_el = card.select_one("p[itemprop=description]")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    loc_el = card.select_one("a.safer_region_link")
    ville = loc_el.get_text(" ", strip=True) if loc_el else ""
    # ville réelle parfois dans la description (« portes de MONTVAL-SUR-LOIR »)
    mville = re.search(r"(?:de|à|portes? de)\s+([A-ZÉÈ][A-ZÉÈ\-]+(?:-[A-ZÉÈ\-]+)*)",
                       description)
    if mville:
        ville = mville.group(1).title()

    price_el = card.select_one("div.res_tbl_value[itemprop=price]")
    prix = None
    if price_el:
        content = price_el.get("content")
        prix = parse_price(content) if content else parse_price(price_el.get_text())

    land_el = card.select_one("b.safer_land_value")
    surface_terrain = _ha_a_ca_to_m2(land_el.get_text(" ", strip=True)) if land_el else None

    photos = []
    a_img = card.select_one("a.res_tbl1[style*=background-image]")
    if a_img:
        murl = re.search(r"url\(([^)]+)\)", a_img.get("style", ""))
        if murl:
            photos.append(murl.group(1).strip("'\""))

    return {
        "source": "safer_annonces",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "propriete",
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": f"{dept}000",  # pseudo-CP (seul le dept est connu)
        "surface": None,              # surface habitable non fournie en liste
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "SAFER",
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "SAFER")

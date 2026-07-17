"""scrapers/deux_b_immobilier.py — 2B Immobilier Conseil (Bourges, 18)

Méthode : scrape_simple (httpx) — CMS **Netty**, gabarit ANCIEN : SSR HTML pur
(cartes div.res_tbl, images img.netty.immo), PAS le template React/base64 des
Netty récents (cf. immo_mais_pas_que). Agence indépendante du centre de Bourges
(+15 ans), stock Cher (18) + une annonce satellite Ramatuelle (83, écartée).

Découverte : le listing « toutes maisons » (/maison-a-vendre-.htm) ne montre que
12 cartes et la pagination (?page=N) est IGNORÉE côté serveur → on agrège les
pages PAR VILLE (/maison-a-vendre-{Ville}.htm, liens découverts sur la page
racine), dédup par réf VMxxx. Les cartes ne portent PAS de code postal → fetch
de la page détail (borné DETAIL_MAX) qui expose CP (itemprop=postalCode),
pièces/chambres, terrain (« 04 a 36 ca ») et « DPE : X ».
POST-FILTRE STRICT code_postal[:2] (bien sans CP résolu écarté) → 0 fuite.

Ne requête que si le 18 est demandé.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import (
    _jitter,
    get_with_retry,
    make_client,
    parse_price_digits,
    standalone_main,
)

BASE_URL = "https://www.2bimmobilierconseil.fr"
SOURCE = "deux_b_immobilier"
LABEL = "2BImmobilier"
AGENCE = "2B Immobilier Conseil"
DEPTS_AGENCE = {"18"}
DETAIL_MAX = 40          # garde-fou : nb max de pages détail visitées
PHOTOS_PER_BIEN = 10

_REF_RE = re.compile(r"_(VM\d+)\.htm")
_VILLE_PAGE_RE = re.compile(r"/maison-a-vendre-[^\"]*\.htm")


def _parse_terrain_aca(text: str) -> float | None:
    """'04 a 36 ca' / '1 ha 20 a' → m² (1 ha=10000, 1 a=100, 1 ca=1)."""
    text = text or ""
    total = 0.0
    for val, unit in re.findall(r"(\d+(?:[.,]\d+)?)\s*(ha|ca|a)\b", text, re.IGNORECASE):
        f = float(val.replace(",", "."))
        total += f * {"ha": 10000.0, "a": 100.0, "ca": 1.0}[unit.lower()]
    return total or None


def _parse_card(card) -> dict | None:
    link = card.select_one("a.res_tbl1") or card.find("a", href=_REF_RE)
    href = link.get("href", "") if link else ""
    m = _REF_RE.search(href)
    if not m:
        return None
    ref = m.group(1)
    url = href if href.startswith("http") else BASE_URL + href

    h2 = card.select_one("h2 a") or card.select_one("h2")
    titre = h2.get_text(" ", strip=True) if h2 else ""

    desc_el = card.select_one("p[itemprop=description]")
    description = re.sub(
        r"\s+", " ", desc_el.get_text(" ", strip=True) if desc_el else ""
    ).strip()

    prix_el = card.select_one("[itemprop=price]")
    prix = None
    if prix_el:
        prix = parse_price_digits(prix_el.get("content") or prix_el.get_text(strip=True))
    if not prix:               # content="0" = « Nous contacter »
        prix = None

    # « Maison individuelle Bourges Croix Moreau  110 m² »
    loc_el = card.select_one(".loc_details")
    loc_text = loc_el.get_text(" ", strip=True) if loc_el else ""
    surface = None
    m_s = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", loc_text)
    if m_s:
        surface = float(m_s.group(1).replace(",", "."))

    photos = []
    style = link.get("style", "") if link else ""
    m_bg = re.search(r"url\((https?://[^)\s]+)\)", style)
    if m_bg:
        photos.append(m_bg.group(1))

    return {
        "source": SOURCE,
        "url": url,
        "id_annonce": ref,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": description[:1200],
        "departement": "",
        "ville": "",
        "code_postal": "",
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": AGENCE,
    }


def _enrich_from_detail(bien: dict, html: str) -> None:
    """Complète CP/ville/pièces/chambres/terrain/DPE/photos depuis la page détail."""
    m = re.search(
        r'itemprop="addressLocality">([^<]+)</span></span>'
        r'<span class="acc">\s*(\d{5})', html)
    if m:
        bien["ville"] = m.group(1).strip()[:80]
        bien["code_postal"] = m.group(2)
        bien["departement"] = m.group(2)[:2]

    m = re.search(r">Pièces</td><td[^>]*>\s*(\d+)", html)
    if m:
        bien["pieces"] = int(m.group(1))
    m = re.search(r'class="gray">\s*(\d+)\s*chambres?', html)
    if m:
        bien["chambres"] = int(m.group(1))

    m = re.search(r"Superficie du terrain</td><td[^>]*>([^<]+)", html)
    if m:
        bien["surface_terrain"] = _parse_terrain_aca(m.group(1))

    if bien.get("surface") is None:
        m = re.search(r">Surface</td><td[^>]*>\s*(\d+(?:[.,]\d+)?)", html)
        if m:
            bien["surface"] = float(m.group(1).replace(",", "."))

    m = re.search(r"DPE\s*:\s*([A-G])\b", html)
    if m:
        bien["dpe"] = m.group(1)

    if not bien.get("prix"):
        m = re.search(r'itemprop="price" content="(\d+)', html)
        if m and m.group(1) != "0":
            bien["prix"] = float(m.group(1))

    photos = list(bien.get("photos") or [])
    for u in re.findall(r"https://img\.netty\.immo/product/[^\"'\)\s]+_l\.jpg", html):
        if u not in photos:
            photos.append(u)
    bien["photos"] = photos[:PHOTOS_PER_BIEN]


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    cibles = departements & DEPTS_AGENCE
    if not cibles:
        return []

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    biens: list[dict] = []
    candidats: dict[str, dict] = {}    # ref -> bien (pré-détail)

    async with make_client() as client:
        # 1) Page racine « toutes maisons » : cartes + liens des pages par ville
        r = await get_with_retry(client, f"{BASE_URL}/maison-a-vendre-.htm")
        if r is None or r.status_code != 200:
            print(f"[{LABEL}] listing racine injoignable")
            return []
        ville_pages = sorted(set(_VILLE_PAGE_RE.findall(r.text)))
        pages_html = [r.text]

        # 2) Pages par ville (le listing racine est tronqué à 12 cartes)
        for path in ville_pages:
            if path.rstrip("/").endswith("maison-a-vendre-.htm"):
                continue
            rv = await get_with_retry(client, BASE_URL + path)
            if rv is not None and rv.status_code == 200:
                pages_html.append(rv.text)
            await asyncio.sleep(_jitter(0.5))

        for html in pages_html:
            for card in BeautifulSoup(html, "html.parser").select("div.res_tbl"):
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if bien and bien["id_annonce"] not in candidats:
                    candidats[bien["id_annonce"]] = bien

        # 3) Pré-filtre prix sur les cartes (évite des fetchs détail inutiles),
        #    puis page détail (bornée) pour le CP — filtre dept STRICT ensuite.
        retenus = []
        for bien in candidats.values():
            p = bien.get("prix") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            retenus.append(bien)

        for bien in retenus[:DETAIL_MAX]:
            rd = await get_with_retry(client, bien["url"])
            if rd is not None and rd.status_code == 200:
                try:
                    _enrich_from_detail(bien, rd.text)
                except Exception:
                    pass
            await asyncio.sleep(_jitter(0.5))

            cp = bien.get("code_postal") or ""
            if cp[:2] not in cibles:   # CP absent ou hors zone → écarté
                continue
            s = bien.get("surface") or 0
            if surface_min and s and s < surface_min:
                continue
            biens.append(bien)

    print(f"[{LABEL}] {len(biens)} annonces (candidats: {len(candidats)})")
    return biens


if __name__ == "__main__":
    standalone_main(search, AGENCE)

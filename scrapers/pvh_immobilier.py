"""scrapers/pvh_immobilier.py — Perche Val d'Huisne Immobilier (Nogent-le-Rotrou)

Méthode : scrape_simple (httpx) — SSR HTML.
Agence du Perche (Nogent-le-Rotrou, 28) — couvre le sud Eure-et-Loir (28) et
déborde sur l'Orne (61, hors zone) et la Sarthe (72, cible).

URL liste maisons (toutes communes du secteur, une seule page) :
    /immobilier.php?recherche_offre=achat&recherche_type_bien[]=maison
Cartes : a.property-card  (div.card à l'intérieur)
  - prix          : .prix                « 91 900 € »
  - ref           : .ref                 « Réf.: PVH 2835 »  → id_annonce
  - localisation  : .lieux               « MALE - 61260 »    → ville + CP
  - type          : .descri-title        « Maison »
  - description   : .descri-detail
  - intérieur/extérieur/pièces/chambres : .card-footer (libellés)
  - data-favorite-* (price/title/ref/image) en secours
  - URL détail    : href (le slug contient déjà ville-CP)

Filtre département : le CP est présent en clair sur la carte (.lieux « VILLE - CP »
et dans le slug d'URL « /ville-28400/ »). POST-FILTRE STRICT code_postal[:2] ∈
départements cibles → écarte notamment les biens de l'Orne (61). 0 fuite.

Interface : async def search(criteres: dict) -> list[dict]
"""
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_int, parse_price, standalone_main

BASE_URL = "https://www.pvh-immobilier.com"
LIST_URL = BASE_URL + "/immobilier.php?recherche_offre=achat&recherche_type_bien%5B%5D=maison"
PHOTOS_PER_CARD = 3


def _parse_card(card) -> dict | None:
    href = card.get("href") or ""
    if not href:
        return None
    # le href contient parfois « &n=..&p=.. » accolé → on coupe au 1er &
    url = href.split("&")[0]
    if not url.startswith("http"):
        url = BASE_URL + url

    loc_el = card.select_one(".lieux")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    m_cp = re.search(r"(\d{5})", loc)
    code_postal = m_cp.group(1) if m_cp else ""
    if not code_postal:
        # repli : CP depuis le slug d'URL (« .../ville-28400/... »)
        m = re.search(r"-(\d{5})/", href)
        code_postal = m.group(1) if m else ""
    ville = re.sub(r"\s*-?\s*\d{5}\s*$", "", loc).strip().title()

    price_el = card.select_one(".prix")
    prix = parse_price(price_el.get_text(" ", strip=True) if price_el else "")
    if prix is None:
        btn = card.select_one("[data-favorite-price]")
        if btn:
            prix = parse_price(btn.get("data-favorite-price", ""))

    ref_el = card.select_one(".ref")
    ref = ""
    if ref_el:
        ref = re.sub(r"^\s*Réf\.?\s*:\s*", "", ref_el.get_text(" ", strip=True), flags=re.I)
    if not ref:
        btn = card.select_one("[data-favorite-ref]")
        ref = btn.get("data-favorite-ref", "") if btn else ""
    id_annonce = ref or url

    type_el = card.select_one(".descri-title")
    type_bien = (type_el.get_text(" ", strip=True) if type_el else "maison").lower()

    desc_el = card.select_one(".descri-detail")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    titre = ""
    img = card.select_one("img")
    if img and img.get("alt"):
        titre = img.get("alt")
    if not titre:
        btn = card.select_one("[data-favorite-title]")
        titre = btn.get("data-favorite-title", "").strip() if btn else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # footer : Intérieur (surface) / Extérieur (terrain) / Pièces / Chb
    footer = card.select_one(".card-footer")
    surface = surface_terrain = pieces = chambres = None
    if footer:
        ftxt = footer.get_text(" ", strip=True)
        m_int = re.search(r"Int[ée]rieur\s+([\d\s\xa0]+)\s*m", ftxt, re.I)
        if m_int:
            try:
                surface = float(re.sub(r"[\s\xa0]", "", m_int.group(1)))
            except ValueError:
                pass
        m_ext = re.search(r"Ext[ée]rieur\s+([\d\s\xa0]+)\s*m", ftxt, re.I)
        if m_ext:
            try:
                surface_terrain = float(re.sub(r"[\s\xa0]", "", m_ext.group(1)))
            except ValueError:
                pass
        pieces = parse_int(r"Pi[èe]ces?\s+(\d+)", ftxt)
        chambres = parse_int(r"Chb\.?\s+(\d+)", ftxt)
    # secours via l'alt de l'image (« ... - 5 pièces - 4 chambres - ... »)
    if pieces is None and img and img.get("alt"):
        pieces = parse_int(r"(\d+)\s*pi[èe]ces?", img.get("alt"))
    if chambres is None and img and img.get("alt"):
        chambres = parse_int(r"(\d+)\s*chambres?", img.get("alt"))

    photos = []
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            photos.append(src if src.startswith("http") else BASE_URL + src)
    if not photos:
        btn = card.select_one("[data-favorite-image]")
        if btn and btn.get("data-favorite-image"):
            photos.append(btn.get("data-favorite-image"))

    return {
        "source": "pvh_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien or "maison",
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Perche Val d'Huisne Immobilier",
    }


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen: set[str] = set()

    async with make_client() as client:
        r = await get_with_retry(client, LIST_URL)
        if r is None or r.status_code != 200:
            print(f"[PVH] Liste inaccessible (status {getattr(r, 'status_code', '?')})")
            return results
        cards = BeautifulSoup(r.text, "html.parser").select("a.property-card")
        kept = {}
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue
            cp = bien.get("code_postal") or ""
            if not cp or cp[:2] not in departements:
                continue
            aid = bien.get("id_annonce")
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
            kept[cp[:2]] = kept.get(cp[:2], 0) + 1

    print(f"[PVH] {len(cards)} cartes → {len(results)} retenues par dept {kept}")
    return results


if __name__ == "__main__":
    standalone_main(search, "Perche Val d'Huisne Immobilier")

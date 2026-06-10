"""scrapers/c_immobilier.py — C Immobilier (La Ferté-Bernard, agence de proximité 72)

Méthode : scrape_simple (httpx) — SSR HTML (site en http, redirige vers https)
URL pattern : moteur de recherche unifié, contenu SSR dans le HTML brut :
    /immobilier.php?recherche_offre=achat&recherche_tri=DISTANCE_ASC&page=N
    → liste TOUTES les annonces « achat » de l'agence (pas de filtre département
      côté serveur fiable → POST-FILTRE STRICT sur le code postal).

Particularité filtre département : l'agence est mono-implantée en Sarthe (72) mais
son stock déborde sur quelques communes limitrophes (28, 41 en zone ; 61 Orne HORS
zone). Le département n'est PAS un paramètre d'URL → on scrape le national agence et
on post-filtre `code_postal[:2] in departements` (0 fuite vérifié).

Cartes : div.single_property.js-property-card (microdata schema.org/Offer)
  - id      : meta[itemprop=id]
  - url+CP  : meta[itemprop=url]  →  /immobilier/{type}/a-vendre/{ville}-{CP}/{type}-{id}
  - prix    : .property_price[content]  (numérique, même si affichage "Prix à la demande")
  - ville   : .property_city
  - type    : .property_type  (Maison / Appartement / Immeuble / Terrain…)
  - titre   : h2  (dans .single_property_text)
  - desc    : meta[itemprop=description] (sinon h3)
  - surf/terr/pieces/chb : bloc .il-card-quickview-item → "Intérieur N m 2",
                           "Extérieur N m 2", "Pièces N", "Chb. N"
  - pieces  : secours via meta[itemprop=name] "… - N pièces - N chambres - …"
  - photos  : img[src] sur photos.c-immobilier.fr

Type de bien : on ne garde que maisons / propriétés (exclut appartement, immeuble,
               terrain, local, parking, garage, fonds de commerce).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.c-immobilier.fr"
SEARCH_URL = BASE_URL + "/immobilier.php?recherche_offre=achat&recherche_tri=DISTANCE_ASC&page={page}"
MAX_PAGES = 25
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien (champ .property_type) à conserver : maisons / propriétés.
_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|mas|g[iî]te|corps de ferme|maison de village",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|terrain|local|commerce|garage|parking|immeuble|"
    r"bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = SEARCH_URL.format(page=page)
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[C-Immobilier] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                "div.single_property"
            )
            if not cards:
                break

            for card in cards:
                try:
                    bien = _parse_card(card, departements)
                except Exception:
                    continue
                if not bien:
                    continue

                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                seen_ids.add(aid)
                results.append(bien)

            await asyncio.sleep(0.6)

    # Post-filtre département STRICT (0 fuite) — déjà appliqué dans _parse_card,
    # filet de sécurité supplémentaire ici.
    results = [
        b for b in results
        if b["code_postal"] and b["code_postal"][:2] in departements
    ]

    by_dept: dict[str, int] = {}
    for b in results:
        d = b["code_postal"][:2]
        by_dept[d] = by_dept.get(d, 0) + 1
    print(f"[C-Immobilier] Total {len(results)} annonces — par dept {by_dept}")

    return results


def _parse_card(card, departements: list[str]) -> dict | None:
    # URL + code postal (depuis l'URL, fiable)
    url_meta = card.select_one("meta[itemprop=url]")
    url = url_meta["content"].strip() if url_meta and url_meta.get("content") else ""
    if not url:
        link = card.select_one("a.link_property")
        url = link.get("href", "") if link else ""
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = BASE_URL + url

    m_cp = re.search(r"-(\d{5})/", url)
    code_postal = m_cp.group(1) if m_cp else ""
    dept = code_postal[:2] if code_postal else ""

    # FILTRE DÉPARTEMENT STRICT — 0 fuite hors-zone
    if not dept or dept not in departements:
        return None

    # Type de bien
    type_el = card.select_one(".property_type")
    type_bien = type_el.get_text(strip=True) if type_el else ""
    if _EXCLUDE_TYPE.search(type_bien) and not _KEEP_TYPE.search(type_bien):
        return None
    if not _KEEP_TYPE.search(type_bien):
        return None

    # id_annonce : référence affichée sinon meta id
    ref = ""
    ref_el = card.select_one(".property_price .ref")
    if ref_el:
        ref = re.sub(r"^R[ée]f\.?\s*:\s*", "", ref_el.get_text(strip=True))
    id_meta = card.select_one("meta[itemprop=id]")
    id_num = id_meta["content"].strip() if id_meta and id_meta.get("content") else ""
    id_annonce = ref or id_num or url

    # Ville
    city_el = card.select_one(".property_city")
    ville = city_el.get_text(" ", strip=True) if city_el else ""

    # Prix : attribut content (numérique) prioritaire
    prix = None
    price_el = card.select_one(".property_price")
    if price_el:
        c = price_el.get("content")
        if c:
            prix = _parse_price(c)
        if prix is None:
            prix = _parse_price(price_el.get_text(" ", strip=True))

    # Titre
    h2 = card.select_one(".single_property_text h2") or card.select_one("h2")
    titre = h2.get_text(" ", strip=True) if h2 else ""
    if not titre:
        titre = f"{type_bien} {ville}".strip()

    # Description
    desc_meta = card.select_one("meta[itemprop=description]")
    description = (
        desc_meta["content"].strip()
        if desc_meta and desc_meta.get("content")
        else ""
    )
    if not description:
        h3 = card.select_one(".single_property_text h3") or card.select_one("h3")
        description = h3.get_text(" ", strip=True) if h3 else ""

    # Surface / terrain / pièces / chambres depuis le bloc quickview
    quick = " ".join(
        el.get_text(" ", strip=True)
        for el in card.select(".il-card-quickview-item")
    )
    card_text = quick or card.get_text(" ", strip=True)
    surface = _parse_m2(r"Int[ée]rieur\s*([\d\s\xa0]+)\s*m", card_text)
    surface_terrain = _parse_m2(r"Ext[ée]rieur\s*([\d\s\xa0]+)\s*m", card_text)
    pieces = _parse_int(r"Pi[èe]ces\s*(\d+)", card_text)
    chambres = _parse_int(r"Chb\.?\s*(\d+)", card_text)

    # Secours pièces/chambres via meta name "… - N pièces - N chambres - …"
    name_meta = card.select_one("meta[itemprop=name]")
    name_txt = (
        name_meta["content"] if name_meta and name_meta.get("content") else ""
    )
    if pieces is None:
        pieces = _parse_int(r"(\d+)\s*pi[èe]ces?", name_txt)
    if chambres is None:
        chambres = _parse_int(r"(\d+)\s*chambres?", name_txt)

    # Photos
    photos = []
    for img in card.select("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and "photos.c-immobilier.fr" in src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "c_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien or "maison",
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "C Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", str(text))
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    # Filtre les valeurs aberrantes (0, prix < 1000 = bruit)
    if v is not None and v < 1000:
        return None
    return v


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_m2(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
    except ValueError:
        return None
    if f <= 0:
        return None
    return f


# ── CLI standalone ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    biens = asyncio.run(
        search(
            {
                "departements": criteres.departements,
                "prix_max": criteres.prix_max,
                "prix_min": getattr(criteres, "prix_min", 0),
                "surface_min": criteres.surface_min,
            }
        )
    )
    print(f"\nTotal C Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

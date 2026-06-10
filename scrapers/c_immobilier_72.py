"""scrapers/c_immobilier_72.py — C Immobilier (agence de proximité, La Ferté-Bernard, Sarthe 72)

Méthode : scrape_simple (httpx) — SSR HTML
Site : https://www.c-immobilier.fr  — agence mono-zone du Perche sarthois
       (presbytères, demeures, longères, maisons de bourg autour de La Ferté-Bernard).

Particularité : les jolies URL « /vente/maison/sarthe/ » sont décoratives et
                redirigent vers la home (pas de listing). Le vrai listing est le
                endpoint paginé immobilier.php :
                  /immobilier.php?recherche_offre=achat&recherche_commune=ferte-aglo
                  &recherche_tri=DISTANCE_ASC&page=N
                Pagination jusqu'à épuisement (les pages au-delà du stock renvoient
                les mêmes cartes → arrêt sur 0 nouveau bien).

Cartes : div.single_property.js-property-card (schema.org/Offer)
  - URL      : meta[itemprop=url][content]  → /immobilier/{type}/a-vendre/{ville-CP}/{type-id}
  - id       : meta[itemprop=id][content]   (id interne) ; réf. dans .ref
  - Prix     : [itemprop=price][content]    (entier en €)
  - Ville    : data-address (ou .property_city)
  - Type     : .property_type               ("Maison", "Appartement", "Immeuble"…)
  - Titre    : h2 (ou meta[itemprop=name])
  - Desc     : meta[itemprop=description][content]
  - Surface  : .il-card-quickview-item "Intérieur" → "108 m2"
  - Terrain  : .il-card-quickview-item "Extérieur" → "480 m2"
  - Pièces   : .il-card-quickview-item "Pièces"
  - Chambres : .il-card-quickview-item "Chb."
  - Photos   : img.full-width[src] / onerror fallback
  - DPE      : non exposé en liste → None
  - Agence   : meta[itemprop=seller] (sinon "C Immobilier")

Filtre département : le code postal est fiablement présent dans le slug d'URL
                     ({ville}-{CP}). Post-filtre STRICT code_postal[:2] ∈ cibles.
                     Agence 100 % Sarthe (72) → court-circuit si 72 hors zone.
On ne garde que maisons / propriétés / longères (pas d'appartement/immeuble/local…).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.c-immobilier.fr"
LISTING_URL = (
    BASE_URL
    + "/immobilier.php?recherche_offre=achat&recherche_commune=ferte-aglo"
    + "&recherche_tri=DISTANCE_ASC&page={page}"
)
MAX_PAGES = 12
PHOTOS_PER_CARD = 10

# Agence 100 % Sarthe : on ne crawle que si 72 fait partie des départements cibles.
DEPT_ZONE = "72"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien à conserver (maisons / propriétés / vieilles pierres)
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|long[èe]re|manoir|ch[âa]teau|moulin|"
    r"demeure|domaine|mas|presbyt[èe]re|g[îi]te|corps.de.ferme|bourg",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|terrain|local|commerce|garage|parking|immeuble|"
    r"bureau|fonds|box",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Mono-zone : inutile de requêter si 72 n'est pas ciblé.
    if DEPT_ZONE not in departements:
        print(f"[CImmo72] Dept {DEPT_ZONE} hors cible → court-circuit")
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()  # toutes les fiches vues (pour détecter l'épuisement)

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = LISTING_URL.format(page=page)
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[CImmo72] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                "div.single_property.js-property-card"
            )
            if not cards:
                break

            new_cards = 0     # fiches inédites (tous types) → mesure d'épuisement
            kept_on_page = 0  # fiches retenues après filtres
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue

                # Suivi d'épuisement de la pagination (indépendant des filtres) :
                # on regarde l'URL brute de la fiche, même si elle sera écartée.
                card_url = ""
                u = card.select_one("meta[itemprop=url]")
                if u and u.get("content"):
                    card_url = u["content"].strip()
                if card_url and card_url not in seen_urls:
                    seen_urls.add(card_url)
                    new_cards += 1

                if not bien:
                    continue

                # Post-filtre département STRICT (0 fuite hors zone cible)
                cp = bien["code_postal"]
                if not cp or cp[:2] not in departements:
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
                kept_on_page += 1

            print(
                f"[CImmo72] Page {page}: {kept_on_page} retenues "
                f"({new_cards} fiches inédites)"
            )
            # On arrête quand la pagination ne ramène plus AUCUNE fiche inédite
            # (le endpoint reboucle sur le même stock une fois épuisé).
            if new_cards == 0:
                break
            await asyncio.sleep(0.5)

    print(f"[CImmo72] Total : {len(results)} annonces")
    return results


def _parse_card(card) -> dict | None:
    url_el = card.select_one("meta[itemprop=url]")
    link_el = card.select_one("a.link_property[href]")
    url = ""
    if url_el and url_el.get("content"):
        url = url_el["content"].strip()
    elif link_el:
        url = link_el.get("href", "").strip()
    if not url:
        return None
    if url.startswith("/"):
        url = BASE_URL + url

    # Type de bien (filtrage maisons/propriétés)
    type_el = card.select_one(".property_type")
    type_raw = type_el.get_text(" ", strip=True) if type_el else ""
    # le slug d'URL donne aussi le type : /immobilier/{type}/a-vendre/...
    m_seg = re.search(r"/immobilier/([^/]+)/a-vendre/", url)
    type_seg = m_seg.group(1).replace("-", " ") if m_seg else ""
    type_probe = f"{type_raw} {type_seg}".strip()
    if _EXCLUDE_TYPE.search(type_probe) and not _KEEP_TYPE.search(type_probe):
        return None
    if not _KEEP_TYPE.search(type_probe):
        return None
    type_bien = (type_raw or type_seg or "maison").lower().strip()

    # Code postal depuis le slug : .../{ville}-{CP}/{type-id}
    cp = ""
    m_cp = re.search(r"-(\d{5})/", url)
    if m_cp:
        cp = m_cp.group(1)

    # Identifiant
    id_el = card.select_one("meta[itemprop=id]")
    ref_el = card.select_one(".ref")
    id_num = id_el["content"].strip() if id_el and id_el.get("content") else ""
    ref = ""
    if ref_el:
        ref = re.sub(r"^R[ée]f\.?\s*:?\s*", "", ref_el.get_text(strip=True))
    id_annonce = id_num or ref or url

    # Ville
    ville = (card.get("data-address") or "").strip()
    if not ville:
        city_el = card.select_one(".property_city")
        ville = city_el.get_text(" ", strip=True) if city_el else ""
    ville = _titlecase_ville(ville)

    # Titre
    title_el = card.select_one("h2") or card.select_one("meta[itemprop=name]")
    if title_el and title_el.name == "meta":
        titre = title_el.get("content", "").strip()
    else:
        titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Description
    desc_el = card.select_one("meta[itemprop=description]")
    if desc_el and desc_el.get("content"):
        description = desc_el["content"].strip()
    else:
        h3 = card.select_one(".single_property_text h3")
        description = h3.get_text(" ", strip=True) if h3 else ""

    # Prix
    price_el = card.select_one("[itemprop=price]")
    prix = None
    if price_el:
        prix = _parse_price(
            price_el.get("content") or price_el.get_text(" ", strip=True)
        )

    # Quickview : Intérieur / Extérieur / Pièces / Chb.
    quick: dict[str, str] = {}
    for q in card.select(".il-card-quickview-item"):
        sp = q.find("span")
        st = q.find("strong")
        if sp and st:
            quick[sp.get_text(strip=True).lower()] = st.get_text(" ", strip=True)

    surface = _parse_m2(quick.get("intérieur") or quick.get("interieur"))
    surface_terrain = _parse_m2(quick.get("extérieur") or quick.get("exterieur"))
    pieces = _parse_int(quick.get("pièces") or quick.get("pieces"))
    chambres = _parse_int(quick.get("chb.") or quick.get("chb"))

    # Photos
    photos = []
    for img in card.select("img.full-width, .block_image img"):
        src = img.get("src") or ""
        if not src or src.startswith("data:") or "fb.png" in src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        photos.append(src)
    photos = list(dict.fromkeys(photos))[:PHOTOS_PER_CARD]

    # Agence
    seller_el = card.select_one("meta[itemprop=seller]")
    agence = (
        seller_el["content"].strip()
        if seller_el and seller_el.get("content")
        else "C Immobilier"
    )

    return {
        "source": "c_immobilier_72",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": cp[:2] if cp else DEPT_ZONE,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": agence,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_m2(text: str | None) -> float | None:
    """'108 m 2' / '480 m2' → 108.0 / 480.0"""
    if not text:
        return None
    m = re.search(r"([\d\s\xa0]+)", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
        return f if f > 0 else None
    except ValueError:
        return None


def _parse_int(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def _titlecase_ville(text: str) -> str:
    if not text:
        return ""
    if text.isupper():
        return text.title()
    return text


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
    print(f"\nTotal C Immobilier 72: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

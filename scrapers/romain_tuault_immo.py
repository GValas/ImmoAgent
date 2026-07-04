"""scrapers/romain_tuault_immo.py — Romain Tuault Immobilier (Craon / Renazé / Château-Gontier, 53)

Méthode : scrape_simple (httpx) — SSR HTML (CMS La Boîte Immo, images staticlbi.com,
gabarit property-listing-v2 comme fnaim_beugnot). Agence indépendante du sud-Mayenne
(3 agences), stock 53 + frange Maine-et-Loire (49) : maisons de maître, propriétés.

URL pattern : /vente/{page} (~10 cartes article.property-listing-v2__container/page,
~70 biens observés ; pagination SANS cookie, vérifié). PAS de filtre département
côté serveur → post-filtre STRICT code_postal[:2] ∈ départements demandés (CP sur
chaque carte : .title__content-1 « La Selle-Craonnaise (53800) »).

Ne requête que si 53 ou 49 est demandé.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import (
    _jitter,
    get_with_retry,
    make_client,
    parse_loc,
    parse_price_digits,
    standalone_main,
)

BASE_URL = "https://www.romaintuaultimmo.fr"
DEPTS_AGENCE = {"53", "49"}   # Mayenne + frange Maine-et-Loire
MAX_PAGES = 15

# Type de bien = segment d'URL : /vente/{NNN-ville}/{type}/{tN}/{id-slug}/
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


def _surface_habitable(txt: str) -> float | None:
    """Première surface « NNN m² » plausible NON précédée de terrain/parcelle/jardin."""
    for m in re.finditer(r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m[²2]", txt or ""):
        avant = (txt[max(0, m.start() - 25):m.start()]).lower()
        if "terrain" in avant or "parcelle" in avant or "jardin" in avant:
            continue
        try:
            v = float(re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", "."))
        except ValueError:
            continue
        if 8 <= v <= 1500:
            return v
    return None


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    cibles = departements & DEPTS_AGENCE
    if not cibles:
        return []

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    biens: list[dict] = []
    seen_ids: set[str] = set()

    async with make_client() as client:
        for page in range(1, MAX_PAGES + 1):
            r = await get_with_retry(client, f"{BASE_URL}/vente/{page}")
            if r is None or r.status_code != 200:
                break
            cards = BeautifulSoup(r.text, "html.parser").select(
                "article.property-listing-v2__container")
            if not cards:
                break

            # L'arrêt de pagination se fonde sur les cartes BRUTES (dédup id),
            # pas sur les biens gardés : avec des bornes prix serrées, des pages
            # entières sont filtrées alors que la suite du stock reste à lire.
            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue
                aid = bien.get("id_annonce") or bien.get("url")
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                new_on_page += 1
                cp = bien.get("code_postal") or ""
                if cp[:2] not in cibles:   # garde-fou département STRICT
                    continue
                bien["departement"] = cp[:2]
                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue
                biens.append(bien)

            if new_on_page == 0:   # page entière déjà vue → fin de la liste
                break
            await asyncio.sleep(_jitter(0.5))

    print(f"[RomainTuault] {len(biens)} annonces")
    return biens


def _parse_card(card) -> dict | None:
    link = card.select_one("a.item__title")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    parts = [p for p in href.split("/") if p]
    # ['vente', '106-la-selle-craonnaise', 'maison', 't5', '2306-maison-de-107-m2']
    type_seg = parts[2] if len(parts) > 2 else ""
    if _EXCLUDE_TYPE.search(type_seg):
        return None
    type_bien = type_seg.replace("-", " ").strip() or "maison"

    loc_el = card.select_one(".title__content-1")
    ville, code_postal = parse_loc(loc_el.get_text(" ", strip=True) if loc_el else "")
    if not code_postal:
        return None

    titre_el = card.select_one(".title__content-2")
    titre = titre_el.get_text(" ", strip=True) if titre_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    prix_el = card.select_one(".item__price")
    prix = parse_price_digits(prix_el.get_text(" ", strip=True)) if prix_el else None

    desc_el = card.select_one(".item__text-block")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Surface : « Maison de 107 m2. » dans le titre (sinon description),
    # en ignorant les mentions de terrain (« Propriété sur un terrain de 1884 m2 »)
    surface = _surface_habitable(titre) or _surface_habitable(description)
    surface_terrain = None
    for txt in (titre, description):
        m = re.search(r"terrain[^0-9]{0,30}(\d[\d\s\xa0]{2,8})\s*m[²2]",
                      txt or "", re.IGNORECASE)
        if m:
            try:
                surface_terrain = float(re.sub(r"[\s\xa0]", "", m.group(1)))
                break
            except ValueError:
                pass

    # Pièces : segment tN de l'URL
    pieces = None
    if len(parts) > 3:
        m = re.match(r"^t(\d+)$", parts[3])
        if m:
            pieces = int(m.group(1))

    ref_el = card.select_one(".item__reference")
    ref = ""
    if ref_el:
        ref = re.sub(r"(?i)^r[ée]f\s*:?\s*", "", ref_el.get_text(strip=True))
    m_id = re.match(r"^(\d+)-", parts[-1]) if parts else None
    id_annonce = (m_id.group(1) if m_id else "") or ref.replace(" ", "") or url

    photos = []
    img = card.select_one("img.decorate__img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src.startswith("//"):
            src = "https:" + src
        if src and not src.startswith("data:"):
            photos.append(src)

    return {
        "source": "romain_tuault_immo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Romain Tuault Immobilier",
    }


if __name__ == "__main__":
    standalone_main(search, "Romain Tuault Immobilier")

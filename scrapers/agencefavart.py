"""scrapers/agencefavart.py — Agence Favart (Joigny, Yonne 89)

Méthode : scrape_simple (httpx) — SSR HTML (plateforme Septeo Real Estate)
Site : https://www.agencefavart.fr — agence indépendante de Joigny, secteur
nord-Yonne (Joigny, Migennes, Saint-Florentin, Aillant-sur-Tholon…).

URL : /fr/ventes  puis  /fr/ventes/{page}  (pagination SSR, 12 biens/page).
Pas de filtre département serveur (agence quasi mono-89) mais POST-FILTRE STRICT
sur code_postal[:2] → 0 fuite (le secteur de Joigny touche l'Aube 10).

Cartes : article.minifiche2
  - Titre/type : .color_titre_minifiche2  → "Maison - 8 pièces 89300 Joigny"
  - CP         : .cp                       → "89300"
  - Commune    : .commune                  → "Joigny"
  - Prix       : .prix                     → "Prix de vente : 349 000 €"
  - Réf        : .reference                → "Réf. : 29980"
  - Détail     : a[href^="/fr/vente/"]     → "/fr/vente/maison-8-pieces-joigny-89300/{ID}"
  La surface (m²) n'est pas dans la carte → extraite du titre/slug quand présente.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_int, parse_price

BASE_URL = "https://www.agencefavart.fr"
MAX_PAGES = 10
PHOTOS_PER_CARD = 1

_EXCLUDE_TYPE = re.compile(
    r"terrain|garage|parking|local|commerce|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_min = criteres.get("prix_min", 0)
    prix_max = criteres.get("prix_max", 0)
    surface_min = criteres.get("surface_min", 0)
    results: list[dict] = []
    seen: set[str] = set()

    async with make_client() as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/fr/ventes" if page == 1 else f"{BASE_URL}/fr/ventes/{page}"
            r = await get_with_retry(client, url)
            if r is None or r.status_code != 200:
                break
            cards = BeautifulSoup(r.text, "html.parser").select("article.minifiche2")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue
                dept = (bien.get("code_postal") or "")[:2]
                if dept not in departements:
                    continue
                aid = bien.get("id_annonce") or bien.get("url")
                if aid in seen:
                    continue
                seen.add(aid)
                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue
                new_on_page += 1
                results.append(bien)

            if new_on_page == 0 and page > 1:
                break
            await asyncio.sleep(0.5)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print(f"[AgenceFavart] {len(results)} annonces — {by_dept}")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one('a[href*="/fr/vente/"]')
    if not link:
        return None
    href = link.get("href", "")
    url = href if href.startswith("http") else BASE_URL + href
    m_id = re.search(r"/([A-Z0-9]{16,})$", href)
    id_annonce = m_id.group(1) if m_id else url

    cp_el = card.select_one(".cp")
    code_postal = cp_el.get_text(strip=True) if cp_el else ""
    if not re.match(r"^\d{5}$", code_postal):
        return None
    com_el = card.select_one(".commune")
    ville = com_el.get_text(strip=True) if com_el else ""

    titre_el = card.select_one(".color_titre_minifiche2")
    titre = titre_el.get_text(" ", strip=True) if titre_el else ""

    # Type depuis le titre / slug
    type_raw = (titre or href).lower()
    type_bien = "maison"
    for t in ("maison", "propriete", "propriété", "longere", "longère", "manoir",
              "chateau", "château", "ferme", "moulin", "villa", "demeure"):
        if t in type_raw:
            type_bien = t
            break
    if _EXCLUDE_TYPE.search(type_raw) and type_bien == "maison" and "maison" not in type_raw:
        return None
    if _EXCLUDE_TYPE.search(type_raw) and not re.search(
            r"maison|propriete|propriété|longere|manoir|chateau|ferme|moulin|villa|demeure", type_raw):
        return None

    prix_el = card.select_one(".prix")
    prix = parse_price(prix_el.get_text(" ", strip=True)) if prix_el else None

    pieces = parse_int(r"(\d+)\s*pi[èe]ces?", titre)
    # surface : parfois dans le titre/slug "200-m2" ou "200 m²"
    surface = None
    m_s = re.search(r"(\d{2,4})\s*m[²2]", titre) or re.search(r"(\d{2,4})-?m2", href)
    if m_s:
        try:
            v = float(m_s.group(1))
            if 8 <= v <= 2000:
                surface = v
        except ValueError:
            pass

    ref_el = card.select_one(".reference")
    ref = None
    if ref_el:
        m = re.search(r"(\d+)", ref_el.get_text())
        if m:
            ref = m.group(1)

    desc_el = card.select_one(".right_minifiche2")
    description = ""
    if desc_el:
        description = re.sub(r"\s+", " ", desc_el.get_text(" ", strip=True))

    photos = []
    img = card.select_one("img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(src)

    return {
        "source": "agencefavart",
        "url": url,
        "id_annonce": ref or id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Agence Favart",
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Agence Favart")

"""scrapers/vivaviager.py — VivaViager (vivaviager.com)

Portail national d'annonces en viager (occupé / libre / nue-propriété). SSR HTML
(WordPress thème immobilier Houzez, cartes rendues serveur — httpx pur, pas de JS).

Méthode : scrape_simple (httpx).
URL pattern : /annonces-viager/page/{N}/   (~30 cartes/page, pagination /page/N/).

Cartes : div.item-listing-wrap
  - Lien   : a[href*='/annonce-viager/{slug}/']
  - Titre  : .item-title   "Appt 3P 76m² – Viager libre – Neuilly-sur-Seine"
  - Prix   : 1er .item-price   "341.250€"  (= bouquet / prix d'achat affiché)
  - Adresse: .item-address     "92200, Neuilly-sur-Seine, FR"  → CODE POSTAL + ville
  - Détails: .item-amenities    "Viager Libre 1 Chambre 1 Salle de bain 76 m²"

Filtre DÉPARTEMENT : pas de filtre serveur fiable → on scrape l'inventaire national
  (pagination) et on POST-FILTRE STRICT par code_postal[:2] (extrait de .item-address).
  → 0 fuite garantie.

100 % viager (le mot "viager" est présent dans titre/type). On ne retient que les
biens de type maison / propriété (les appartements sont écartés via le titre).
`prix` = montant .item-price (bouquet / prix d'achat). Le type de viager et le nombre
de chambres sont reportés en description.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.vivaviager.com"
LISTING_PATH = "/annonces-viager"
MAX_PAGES = 20  # garde-fou

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_KEEP_TYPE = re.compile(
    r"maison|villa|propri[ée]t[ée]|ferme|long[èe]re|manoir|chateau|ch[âa]teau|"
    r"moulin|demeure|domaine|mas|pavillon|corps[- ]de[- ]ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"\bappt\b|appartement|terrain|garage|parking|immeuble|local|commerce|"
    r"bureau|fonds|cave|box|studio|loft|duplex",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = (
                f"{BASE_URL}{LISTING_PATH}/"
                if page == 1
                else f"{BASE_URL}{LISTING_PATH}/page/{page}/"
            )
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[VivaViager] ERR page {page}: {e}")
                break
            if r.status_code != 200:
                break

            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select(".item-listing-wrap")
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
                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                new_on_page += 1

                # POST-FILTRE département STRICT
                cp = bien["code_postal"]
                if not cp or cp[:2] not in departements:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                results.append(bien)

            if new_on_page == 0:
                break
            await asyncio.sleep(0.5)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print(f"[VivaViager] total: {len(results)} biens (zone cible) — par dept: {by_dept}")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a[href*='/annonce-viager/']")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    slug = href.rstrip("/").rsplit("/", 1)[-1]
    id_annonce = slug or url

    # Titre
    title_el = card.select_one(".item-title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    # Type de bien (depuis le titre)
    type_src = titre or slug.replace("-", " ")
    if _EXCLUDE_TYPE.search(type_src) and not _KEEP_TYPE.search(type_src):
        return None
    m_t = _KEEP_TYPE.search(type_src)
    if not m_t:
        return None  # appartement / type non maison → exclu
    type_bien = m_t.group(0).lower()

    # Adresse : "92200, Neuilly-sur-Seine, FR"
    addr_el = card.select_one(".item-address") or card.select_one("address")
    code_postal = ""
    ville = ""
    if addr_el:
        addr = addr_el.get_text(" ", strip=True)
        m_cp = re.search(r"\b(\d{5})\b", addr)
        if m_cp:
            code_postal = m_cp.group(1)
        m_v = re.search(r"\d{5},\s*([^,]+)", addr)
        if m_v:
            ville = m_v.group(1).strip()
    if not code_postal:
        return None
    dept = code_postal[:2]

    # Prix : 1er .item-price ("341.250€")
    prix = None
    price_el = card.select_one(".item-price")
    if price_el:
        prix = _parse_price(price_el.get_text(" ", strip=True))

    # Détails : type viager, chambres, surface
    amen_el = card.select_one(".item-amenities")
    amen = amen_el.get_text(" ", strip=True) if amen_el else ""
    full = card.get_text(" ", strip=True)

    chambres = None
    m_ch = re.search(r"(\d+)\s*Chambre", amen or full, re.IGNORECASE)
    if m_ch:
        chambres = int(m_ch.group(1))

    surface = None
    m_s = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", titre or amen or full)
    if m_s:
        try:
            surface = float(m_s.group(1).replace(",", "."))
        except ValueError:
            surface = None

    pieces = None
    m_p = re.search(r"(\d+)\s*P\b", titre)  # "Appt 3P" / "Maison 5P"
    if m_p:
        pieces = int(m_p.group(1))

    # Type de viager → description
    desc_parts = []
    m_vt = re.search(
        r"(Viager Occup[ée]|Viager Libre|Nue[- ]propri[ée]t[ée]|Vente [àa] terme)",
        amen or full,
        re.IGNORECASE,
    )
    if m_vt:
        desc_parts.append(m_vt.group(1))
    if chambres:
        desc_parts.append(f"{chambres} chambre(s)")
    description = " — ".join(desc_parts)

    # Photo
    photos = []
    img = card.select_one("img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(src if src.startswith("http") else BASE_URL + src)

    return {
        "source": "vivaviager",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien[:60],
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:1],
        "dpe": None,
        "agence": "VivaViager",
    }


def _parse_price(text: str) -> float | None:
    # "341.250€" → 341250 ; le point est ici un séparateur de milliers
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

    async def _test():
        depts = ["72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"]
        biens = await search(
            {"departements": depts, "prix_max": 0, "prix_min": 0, "surface_min": 0}
        )
        print(f"\nTotal VivaViager (zone): {len(biens)} biens")
        depts_vus = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
        print(f"Départements vus : {depts_vus}")
        for b in biens[:10]:
            print(
                f"  [{b['code_postal']}] {b['titre'][:50]} — {b['prix']}€"
                f" — {b.get('surface') or '?'}m² — {b['ville']}"
            )

    asyncio.run(_test())

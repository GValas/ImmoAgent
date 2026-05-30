"""scrapers/groupe_mercure.py — Groupe Mercure (demeures de caractère, châteaux, manoirs)

Méthode : scrape_simple (httpx) — SSR WordPress.
Inventaire NATIONAL gérable (~700 annonces, ~12/page, ~59 pages).
Pas de filtre département serveur fiable → on scrape tout le listing national
et on POST-FILTRE par code_postal[:2] (extrait du slug d'URL).

Listing : https://www.groupe-mercure.fr/annonces/  (pagination /annonces/page/N/)
Cards   : div.card.card-annonce (id="post-NNNNN")
  - URL/titre : a.stretched-link  (titre "Château à Bourgoin-Jallieu (38)")
  - code postal : dans le slug d'URL  …-{dept-nom}-{CP5}-{ids}/
  - prix   : span.wpcs_price[data-amount]
  - surface: span.card-annonce__bottom__surface  ("600 m²")
  - pièces : span.card-annonce__bottom__nb_piece  ("11 pièces")
  - photo  : img.card-annonce__image[data-lazy-src]

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.groupe-mercure.fr"
LISTING_URL = f"{BASE_URL}/annonces/"
MAX_PAGES = 70           # plafond de sécurité (~59 pages réelles)
PHOTOS_PER_CARD = 1      # 1 photo de couverture dispo sur la liste

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Mots-clés titre → type de bien (exclut les appartements)
_EXCLUDE_KEYWORDS = re.compile(r"appartement|studio|terrain\b|garage|parking", re.IGNORECASE)
_TYPE_MAP = [
    (re.compile(r"château", re.IGNORECASE), "château"),
    (re.compile(r"manoir", re.IGNORECASE), "manoir"),
    (re.compile(r"villa", re.IGNORECASE), "villa"),
    (re.compile(r"propriété|demeure", re.IGNORECASE), "propriété"),
    (re.compile(r"maison", re.IGNORECASE), "maison"),
    (re.compile(r"hôtel particulier", re.IGNORECASE), "hôtel particulier"),
]


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    all_cards = await _fetch_all_cards()

    results: list[dict] = []
    seen: set[str] = set()
    for card in all_cards:
        bien = _parse_card(card)
        if not bien:
            continue

        # POST-FILTRE département via code_postal[:2]
        cp = bien.get("code_postal") or ""
        dept = cp[:2] if len(cp) >= 2 else ""
        if departements and dept not in departements:
            continue
        bien["departement"] = dept

        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        aid = bien.get("id_annonce") or bien.get("url")
        if aid in seen:
            continue
        seen.add(aid)
        results.append(bien)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[GroupeMercure] Dept {dept}: {n} annonces")

    return results


async def _fetch_all_cards() -> list:
    cards = []
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25) as client:
        for page in range(1, MAX_PAGES + 1):
            url = LISTING_URL if page == 1 else f"{LISTING_URL}page/{page}/"
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    break
            except Exception as e:
                print(f"[GroupeMercure] Erreur page {page}: {e}")
                break

            soup = BeautifulSoup(r.text, "html.parser")
            page_cards = soup.select("div.card.card-annonce")
            if not page_cards:
                break
            cards.extend(page_cards)

            # Dernière page atteinte si pas de lien "page suivante"
            if not soup.select_one(f'a[href*="/annonces/page/{page + 1}/"]'):
                break

            await asyncio.sleep(0.4)

    return cards


def _parse_card(card) -> dict | None:
    try:
        a = card.select_one("a.stretched-link")
        if not a or not a.get("href"):
            return None
        url = a["href"].strip()
        titre = a.get_text(" ", strip=True)

        if _EXCLUDE_KEYWORDS.search(titre):
            return None

        # code postal depuis le slug : …-{nom}-{CP5}-{id}/
        m_cp = re.search(r"-(\d{5})-\d+", url)
        code_postal = m_cp.group(1) if m_cp else None
        if not code_postal:
            # fallback : tout CP5 dans le slug
            m_cp2 = re.search(r"(\d{5})", url)
            code_postal = m_cp2.group(1) if m_cp2 else None

        # ville : "Château à Bourgoin-Jallieu (38)" → "Bourgoin-Jallieu"
        ville = None
        m_v = re.search(r"\bà\s+(.+?)\s*\(\d", titre)
        if m_v:
            ville = m_v.group(1).strip()

        # id annonce depuis id="post-NNNNN"
        id_annonce = None
        cid = card.get("id", "")
        m_id = re.search(r"post-(\d+)", cid)
        if m_id:
            id_annonce = m_id.group(1)

        # type de bien
        type_bien = "maison"
        for rx, label in _TYPE_MAP:
            if rx.search(titre):
                type_bien = label
                break

        # prix
        prix = None
        prix_el = card.select_one(".wpcs_price")
        if prix_el and prix_el.get("data-amount"):
            try:
                prix = float(prix_el["data-amount"])
            except (ValueError, TypeError):
                prix = None
        if prix is None and prix_el:
            prix = _parse_num(prix_el.get_text(" ", strip=True))

        # surface
        surface = None
        surf_el = card.select_one(".card-annonce__bottom__surface")
        if surf_el:
            surface = _parse_num(surf_el.get_text(" ", strip=True))

        # pièces
        pieces = None
        pc_el = card.select_one(".card-annonce__bottom__nb_piece")
        if pc_el:
            m_pc = re.search(r"(\d+)", pc_el.get_text())
            if m_pc:
                pieces = int(m_pc.group(1))

        # photo de couverture
        photos = []
        img = card.select_one("img.card-annonce__image")
        if img:
            src = img.get("data-lazy-src") or img.get("src") or ""
            if src.startswith("http"):
                photos.append(src)
        photos = photos[:PHOTOS_PER_CARD]

        # nettoyage titre (le <br> casse l'espace)
        titre = re.sub(r"\s+", " ", titre).strip()

        return {
            "source": "groupe_mercure",
            "url": url,
            "id_annonce": id_annonce,
            "titre": titre[:150],
            "type_bien": type_bien,
            "description": None,
            "departement": (code_postal or "")[:2],
            "ville": ville,
            "code_postal": code_postal,
            "surface": surface,
            "surface_terrain": None,
            "pieces": pieces,
            "chambres": None,
            "prix": prix,
            "dpe": None,
            "photos": photos,
            "agence": "Groupe Mercure",
        }
    except Exception:
        return None


def _parse_num(text: str) -> float | None:
    """'1 700 000 €' / '600 m²' → float"""
    cleaned = re.sub(r"[^\d,\.]", "", text.replace("\xa0", " ").replace(" ", ""))
    cleaned = cleaned.replace(",", ".")
    # garde un seul point décimal
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    biens = asyncio.run(
        search({
            "departements": criteres.departements,
            "prix_max": criteres.prix_max,
            "prix_min": getattr(criteres, "prix_min", 0),
            "surface_min": criteres.surface_min,
        })
    )
    print(f"\nTotal Groupe Mercure (depts cibles): {len(biens)} annonces")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:60]}"
            f" — {b['prix']}€ — {b.get('surface', '?')}m²"
            f" — {b['ville']} ({b['type_bien']})"
        )

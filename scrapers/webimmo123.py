"""scrapers/webimmo123.py — 123webimmo (réseau de mandataires immobiliers)

Méthode : scrape_simple (httpx) — SSR HTML
URL listing : /liste-biens?page=N  (inventaire NATIONAL, ~48 cartes/page, ~56 pages)
Cartes : article.card--property
  - URL    : h3.card__title > a[href]   (.../vente-achat/{type}-{surf}m2-a-{ville}-{cp}/{id})
  - Titre  : h3.card__title a            (ex. "Maison", "Immeuble")
  - Prix   : p.card__price               ("479 000 €")
  - Résumé : p.card__price .show-for-sr  (description courte)
  - Loc    : p.card__location            ("le mans (72000)")  → ville + code postal
  - Tags   : ul.card__tags li.label--tag ("10 p", "526 m²")   → pieces, surface
  - Photos : figure.lazy-image img[data-src]

Filtre département : le site n'expose PAS de filtre dept/slug serveur fiable
(le filtre coordonnées/postalCode renvoie "Aucun résultat" en httpx). On adopte
donc l'approche remax/era : on parcourt l'inventaire national paginé et on
POST-FILTRE par code_postal[:2] ∈ departements. Le code postal figure dans
chaque carte ET dans l'URL de la fiche, le filtrage est donc fiable.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.123webimmo.com"
LIST_URL = f"{BASE_URL}/liste-biens"
MAX_PAGES = 60          # garde-fou (inventaire ~56 pages observé)
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Mots-clés de type "maison / propriété" dans le titre ou l'URL
_HOUSE_KEYWORDS = re.compile(
    r"maison|villa|longère|ferme|manoir|château|chateau|moulin|propriété|propriete|"
    r"demeure|corps de ferme|gîte|gite|mas|immeuble|domaine|pavillon",
    re.IGNORECASE,
)
_EXCLUDE_KEYWORDS = re.compile(
    r"appartement|appart\b|studio|parking|box\b|cave\b|terrain\b|local|garage",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = LIST_URL if page == 1 else f"{LIST_URL}?page={page}"
            try:
                r = await client.get(url)
                r.raise_for_status()
            except Exception as e:
                print(f"[Webimmo123] Erreur page {page}: {e}")
                break

            cards = _parse_page(r.text)
            if not cards:
                break  # plus d'annonces

            for bien in cards:
                cp = bien.get("code_postal") or ""
                dept = cp[:2] if len(cp) >= 2 else ""
                if departements and dept not in departements:
                    continue
                bien["departement"] = dept

                # Filtres prix / surface
                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                aid = bien.get("id_annonce")
                if aid and aid in seen_ids:
                    continue
                if aid:
                    seen_ids.add(aid)
                results.append(bien)

            await asyncio.sleep(0.4)

    # Log par département
    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[Webimmo123] Dept {dept}: {n} annonces")

    return results


def _parse_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for card in soup.select("article.card--property"):
        try:
            bien = _parse_card(card)
            if bien:
                out.append(bien)
        except Exception:
            continue
    return out


def _parse_card(card) -> dict | None:
    title_a = card.select_one("h3.card__title a[href]")
    if not title_a:
        return None
    url = title_a.get("href", "").strip()
    if url and not url.startswith("http"):
        url = BASE_URL + url

    titre = title_a.get_text(" ", strip=True)

    # Filtre type de bien (titre + URL)
    haystack = f"{titre} {url}".lower()
    if _EXCLUDE_KEYWORDS.search(haystack) and not _HOUSE_KEYWORDS.search(titre):
        return None

    # id_annonce : dernier segment numérique de l'URL
    id_annonce = None
    m_id = re.search(r"/(\d+)(?:[/?#]|$)", url)
    if m_id:
        id_annonce = m_id.group(1)

    # Localisation : "le mans (72000)"
    ville = None
    code_postal = None
    loc_el = card.select_one("p.card__location")
    if loc_el:
        loc_text = loc_el.get_text(" ", strip=True)
        m_cp = re.search(r"\((\d{5})\)", loc_text)
        if m_cp:
            code_postal = m_cp.group(1)
        m_ville = re.match(r"^(.+?)\s*\(", loc_text)
        if m_ville:
            ville = m_ville.group(1).strip().title()

    # Si pas de CP dans la carte, tenter dans l'URL (...-a-{ville}-{cp}/{id})
    if not code_postal and url:
        m_cp2 = re.search(r"-(\d{5})/\d+", url)
        if m_cp2:
            code_postal = m_cp2.group(1)

    # Prix
    prix = None
    price_el = card.select_one("p.card__price")
    if price_el:
        # garder uniquement le noeud texte du prix (avant la tooltip)
        price_text = "".join(
            t for t in price_el.find_all(string=True, recursive=False)
        )
        prix = _parse_price(price_text or price_el.get_text(" ", strip=True))

    # Description courte (tooltip lecteur d'écran)
    description = None
    sr = card.select_one("p.card__price .show-for-sr")
    if sr:
        description = sr.get_text(" ", strip=True) or None

    # Tags : pièces, surface
    surface = None
    pieces = None
    for tag in card.select("ul.card__tags li.label--tag"):
        t = tag.get_text(" ", strip=True)
        m_surf = re.search(r"([\d\s\xa0.,]+)\s*m²", t)
        if m_surf and surface is None:
            surface = _parse_float(m_surf.group(1))
            continue
        m_p = re.search(r"^(\d+)\s*p\b", t)
        if m_p and pieces is None:
            pieces = int(m_p.group(1))

    # Type de bien depuis l'URL (segment avant -NNNm2)
    type_bien = "maison"
    m_type = re.search(r"/vente-achat/([a-z-]+?)-\d+m2", url)
    if m_type:
        type_bien = m_type.group(1).replace("-", " ")
    elif titre:
        type_bien = titre.lower()

    # Photos
    photos = []
    for img in card.select("figure.lazy-image img"):
        src = img.get("data-src") or img.get("src")
        if src and src.startswith("http"):
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    if not titre:
        titre = f"{type_bien.title()} {('à ' + ville) if ville else ''}".strip()

    return {
        "source": "webimmo123",
        "url": url or None,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description,
        "departement": (code_postal or "")[:2] or None,
        "ville": ville,
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "123webimmo",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text or "").replace(",", ".")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_float(text: str) -> float | None:
    val = re.sub(r"[\s\xa0]", "", text or "").replace(",", ".")
    try:
        return float(val) if val else None
    except ValueError:
        return None


# ── CLI standalone ────────────────────────────────────────────────────────────

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
    print(f"\nTotal 123webimmo: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens})
    print(f"Départements vus: {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface', '?')}m²"
            f" — {b.get('pieces', '?')}p"
            f" — {b['ville']} ({b['code_postal']})"
        )

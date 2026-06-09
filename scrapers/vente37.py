"""scrapers/vente37.py — Vente37 / Gautard Immobilier (Touraine, Indre-et-Loire 37)

Méthode : scrape_simple (httpx) — SSR HTML statique.

Portail mono-agence (Gautard Immobilier + partenaires Tours'N Gestion) couvrant
EXCLUSIVEMENT l'Indre-et-Loire (37) — le domaine lui-même (« vente37 ») et toutes
les URL de ville (/avendre/37-{ville}.html, /acheter/37-{ville}.html) sont
préfixées « 37- ». Aucun bien hors-37 ne peut apparaître.

Pages de villes (/avendre/37-*.html, /acheter/37-*.html) : ce sont des landing
pages SEO sans annonce. Les vraies annonces sont sur :
    /BIEN/maison.html        (maisons)
    /BIEN/appartement.html   (appartements)
chacune listant les cartes div.card → lien ../ALAUNE/{type}-{ref}/avendre.html.

Cartes : div.card (sous /BIEN/maison.html)
  - URL    : a[href*='avendre.html']  → page détail
  - Prix   : .price span  →  "232.000" (+ « € »)
  - Statut : .status .meta-list li  →  "Acheter" (on ne garde que la vente)
  - Titre  : .content-wrap .title h2  →  "Maison F5 ... | Chambray-lès-Tours"
             (ville = segment après le dernier « | »)
  - Surface/pièces/chambres : .meta-box-list (surface en m², icônes pers/parking)
  - Réf    : .meta-list  →  "... | Réf : TNG-CG42"
  - Photo  : .img-wrap img[src]  (relative → préfixée BASE_URL/BIEN/)

Filtre département : le site est mono-37 → departement forcé à "37". Pas de code
postal exposé (ni carte ni page détail) → post-filtre STRICT sur departement == "37"
(le bien n'est conservé que si "37" est dans les départements demandés). 0 fuite
possible par construction.

Couverture : très petit stock (une dizaine d'annonces au total), une seule agence.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://vente37.fr"
# Pages listant réellement les annonces (les pages de villes sont du SEO vide)
LISTING_PAGES = ["/BIEN/maison.html", "/BIEN/appartement.html"]
PHOTOS_PER_CARD = 5
DEPT = "37"  # site mono-département (Indre-et-Loire)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_EXCLUDE_TYPE = re.compile(
    r"terrain|local|commerce|garage|parking|immeuble|bureau|fonds", re.IGNORECASE
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    # Site mono-37 : inutile de requêter si 37 n'est pas demandé (0 fuite garantie)
    if DEPT not in departements:
        print(f"[Vente37] Dept 37 hors zone demandée ({departements}) — skip")
        return results

    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page_path in LISTING_PAGES:
            try:
                biens = await _scrape_page(
                    client, page_path, prix_max, prix_min, surface_min, seen_ids
                )
                results.extend(biens)
                print(f"[Vente37] {page_path}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Vente37] Erreur {page_path}: {e}")
            await asyncio.sleep(0.5)

    print(f"[Vente37] Dept 37: {len(results)} annonces")
    return results


async def _scrape_page(
    client: httpx.AsyncClient,
    page_path: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
    seen_ids: set[str],
) -> list[dict]:
    biens: list[dict] = []

    r = await client.get(BASE_URL + page_path)
    if r.status_code != 200:
        return biens

    cards = BeautifulSoup(r.text, "html.parser").select("div.card")
    for card in cards:
        try:
            bien = _parse_card(card)
        except Exception:
            continue
        if not bien:
            continue

        # Post-filtre STRICT : on n'accepte que le département 37
        if bien["departement"] != DEPT:
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
        biens.append(bien)

    return biens


def _parse_card(card) -> dict | None:
    link = card.select_one("a[href*='avendre.html']")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = _abs_url(href)

    # Type de bien depuis le segment d'URL : ../ALAUNE/{type}-{ref}/avendre.html
    seg = [p for p in href.split("/") if p and p != ".."]
    type_seg = seg[1] if len(seg) > 1 else ""
    m_type = re.match(r"([a-zA-Zéèêà]+)", type_seg)
    type_bien = (m_type.group(1).lower() if m_type else "maison")
    if _EXCLUDE_TYPE.search(type_bien):
        return None

    # Statut : on ne garde que la vente ("Acheter"), pas la location
    status_el = card.select_one(".status")
    status_txt = status_el.get_text(" ", strip=True).lower() if status_el else ""
    if "louer" in status_txt or "location" in status_txt:
        return None

    # Titre + ville (segment après le dernier « | »)
    title_el = card.select_one(".content-wrap .title h2") or card.select_one("h2")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    titre = re.sub(r"\s+", " ", titre).strip()
    ville = ""
    if "|" in titre:
        ville = titre.split("|")[-1].strip()
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", ville).strip()

    # Référence (id_annonce) depuis le texte « Réf : XXX » ou le segment d'URL
    ref = ""
    meta_txt = card.get_text(" ", strip=True)
    m_ref = re.search(r"R[ée]f\s*:?\s*([A-Z0-9\-]+)", meta_txt)
    if m_ref:
        ref = m_ref.group(1).strip()
    if not ref:
        m_seg = re.search(r"-([A-Z0-9\-]+)$", type_seg)
        ref = m_seg.group(1) if m_seg else type_seg or url
    id_annonce = ref

    # Prix
    price_el = card.select_one(".price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Surface / pièces / chambres depuis .meta-box-list
    box = card.select_one(".meta-box-list")
    box_txt = box.get_text(" ", strip=True) if box else ""
    surface = _parse_surface(box_txt)
    # Format : "{surface} m² {chambres} {parkings}" (le « 2 » de m² + valeurs)
    # On retire la mention de surface (« 80 m 2 ») puis on lit chambres = 1ʳᵉ valeur.
    rest = re.sub(r"[\d,\s\xa0]+m\s*[²2]", " ", box_txt)
    nums = re.findall(r"\b(\d{1,2})\b", rest)
    chambres = int(nums[0]) if nums else None
    # Pièces : déduit du « F5 » du titre si présent
    pieces = None
    m_f = re.search(r"\b[FT](\d{1,2})\b", titre)
    if m_f:
        pieces = int(m_f.group(1))

    # Photo (relative au dossier /BIEN/)
    photos = []
    img = card.select_one(".img-wrap img") or card.find("img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(_abs_img(src))
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "vente37",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": DEPT,
        "ville": ville[:80],
        "code_postal": None,  # non exposé par le site (mono-37 garanti par construction)
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Gautard Immobilier (Vente37)",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _abs_url(href: str) -> str:
    if href.startswith("http"):
        return href
    href = href.lstrip("./")
    return f"{BASE_URL}/{href}"


def _abs_img(src: str) -> str:
    if src.startswith("http"):
        return src
    if src.startswith("//"):
        return "https:" + src
    src = src.lstrip("./")
    # Les images des cartes /BIEN/ sont relatives à /BIEN/
    if not src.startswith("BIEN/") and not src.startswith("img/"):
        return f"{BASE_URL}/{src}"
    if src.startswith("img/"):
        return f"{BASE_URL}/BIEN/{src}"
    return f"{BASE_URL}/{src}"


def _parse_price(text: str) -> float | None:
    # "232.000 €" : le point est un séparateur de milliers
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = cleaned.replace(".", "").replace(",", "")
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'80 m 2 2 3' → 80.0 (1ère mention en m²)."""
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0]*)\s*m\s*[²2]", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 5 <= f <= 5000:
                return f
        except ValueError:
            pass
    return None


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
    print(f"\nTotal Vente37: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

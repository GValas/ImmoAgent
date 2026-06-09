"""scrapers/inova_immo.py — Inova Immobilier (réseau d'agences Bretagne)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress, thème RealHomes "Ultra").
Site Apache, status 200, pas de Cloudflare.

URL pattern liste : /acheter/                (page 1)
                    /acheter/page/{N}/        (pages suivantes — RealHomes)
  ⚠️ Stock très faible (Bretagne seulement) : la pagination renvoie en pratique
     la même page ; on déduplique par URL et on s'arrête dès qu'aucune nouvelle
     fiche n'apparaît.

Fiches : /bien/{type}-{ref}-inova-{CP}[-N]/  → le CODE POSTAL est DANS l'URL
         (ex: /bien/maison-1339-inova-35800/). On extrait aussi le CP de
         l'adresse de la carte ("56100 Lorient") pour fiabilité.

Filtre département : Inova ne couvre que la Bretagne (35, 29, 22, 56) — aucun
des départements cibles Val-de-Loire (72, 28, 45, 89…). Pas de filtre serveur
par département : on scrape /acheter/ et on POST-FILTRE strict sur
code_postal[:2] ∈ departements (objectif 0 fuite hors-zone).

Cartes : div.rh-ultra-property-card
  - URL   : a.rh-permalink[href]
  - Titre : h3  (ou data-rhea-map-title sur le lien adresse)
  - Adr.  : a[data-rhea-map-title] → texte "CP Ville"
  - Type  : .rh-ultra-property-types  → "Maison" / "Appartement" / "Terrain"…
  - Prix  : .ere-price-display  → "295 400 €"
  - Métas : .rh-ultra-prop-card-meta  (data-tooltip "Chambre(s)"/"Surface"/…)
  - Photos: a.rh-permalink img[data-lazy-src]  (+ .rh-property-images-load span[data-src])

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.inova-immo.com"
MAX_PAGES = 5
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien à conserver (maisons / propriétés). Le reste est exclu.
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|"
    r"maison-de-village|maison-traditionnelle",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"^t\d+$",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_urls: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/acheter/" if page == 1 else f"{BASE_URL}/acheter/page/{page}/"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Inova] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                "div.rh-ultra-property-card"
            )
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card, departements)
                except Exception:
                    continue
                if not bien:
                    continue

                if bien["url"] in seen_urls:
                    continue
                seen_urls.add(bien["url"])
                new_on_page += 1

                # Post-filtre département STRICT (0 fuite hors-zone)
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

            # Pagination RealHomes renvoyant la même page sur petit stock →
            # aucune nouvelle fiche → on arrête.
            if new_on_page == 0:
                break
            await asyncio.sleep(0.5)

    print(f"[Inova] {len(results)} annonces retenues (zone cible)")
    return results


def _parse_card(card, departements: list[str]) -> dict | None:
    link = card.select_one("a.rh-permalink[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # CP : prioritairement depuis l'adresse de la carte, sinon depuis l'URL
    addr_el = card.find(attrs={"data-rhea-map-title": True})
    addr_text = addr_el.get_text(" ", strip=True) if addr_el else ""
    code_postal, ville = _parse_addr(addr_text)
    if not code_postal:
        m = re.search(r"-inova-(\d{5})", href)
        if m:
            code_postal = m.group(1)

    # Type de bien : badge + segment d'URL
    type_el = card.select_one(".rh-ultra-property-types")
    type_txt = type_el.get_text(" ", strip=True) if type_el else ""
    url_type_seg = ""
    m_seg = re.search(r"/bien/([a-z0-9\-]+?)-(?:[a-z]*\d+)-inova", href, re.IGNORECASE)
    if m_seg:
        url_type_seg = m_seg.group(1)
    type_blob = f"{type_txt} {url_type_seg}"
    if _EXCLUDE_TYPE.search(url_type_seg) and not _KEEP_TYPE.search(type_blob):
        return None
    if not _KEEP_TYPE.search(type_blob):
        return None
    type_bien = (type_txt or url_type_seg.replace("-", " ")).strip().lower() or "maison"

    # Référence / id annonce depuis l'URL (.../{type}-{ref}-inova-{cp})
    id_annonce = ""
    m_ref = re.search(r"-([a-z]*\d+)-inova-\d{5}", href, re.IGNORECASE)
    if m_ref:
        id_annonce = m_ref.group(1)
    if not id_annonce:
        id_annonce = url

    # Titre
    title_el = card.select_one("h3") or card.select_one("h2")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre and addr_el and addr_el.get("data-rhea-map-title"):
        titre = addr_el.get("data-rhea-map-title", "")
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Prix
    price_el = card.select_one(".ere-price-display")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")
    if prix is None and addr_el and addr_el.get("data-rhea-map-price"):
        prix = _parse_price(addr_el.get("data-rhea-map-price", ""))

    # Métas (chambres / surface / pièces)
    chambres = surface = pieces = None
    for meta in card.select(".rh-ultra-prop-card-meta"):
        icon = meta.select_one("[data-tooltip]")
        fig = meta.select_one(".figure")
        if not icon or not fig:
            continue
        tip = (icon.get("data-tooltip") or "").lower()
        val = _to_num(fig.get_text(strip=True))
        if val is None:
            continue
        if "chambre" in tip:
            chambres = int(val)
        elif "surface" in tip:
            surface = val
        elif "pièce" in tip or "piece" in tip:
            pieces = int(val)

    # Photos
    photos: list[str] = []
    img = card.select_one("a.rh-permalink img")
    if img:
        src = img.get("data-lazy-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            photos.append(src)
    for sp in card.select(".rh-property-images-load span[data-src]"):
        src = sp.get("data-src") or ""
        if src and not src.startswith("data:") and src not in photos:
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    dept = code_postal[:2] if code_postal else None

    return {
        "source": "inova_immo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Inova Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_addr(text: str) -> tuple[str, str]:
    """'56100 Lorient' → ('56100', 'Lorient')"""
    if not text:
        return "", ""
    m = re.search(r"(\d{5})\s*(.*)$", text)
    if m:
        return m.group(1), m.group(2).strip()
    return "", text.strip()


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text.split("/")[0] if "/" in text else text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _to_num(text: str) -> float | None:
    cleaned = re.sub(r"[^\d.,]", "", text).replace(",", ".")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
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
    print(f"\nTotal Inova: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

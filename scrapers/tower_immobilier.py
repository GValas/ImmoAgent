"""scrapers/tower_immobilier.py — Tower Immobilier (réseau national de mandataires)

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /vente/{page}   (ex: /vente/1, /vente/2 ... ~21 pages)
              → AUCUN filtre département côté serveur (pas de slug/param dept
                fiable) : on scrape le national et on POST-FILTRE sur code_postal[:2].

Cartes : article.item  (21 par page)
  - URL   : a.links-group__link[href]  → /vente/{cityid-slug}/{type}/{id-slug}
  - Ville : .item__block--city  →  "Saint-Saud-Lacoussière (24470)"
  - Type  : 1er mot de .item__title (Maison / Appartement / Villa / Terrain...)
            (confirmé aussi par le segment {type} de l'URL détail)
  - Détail: .item__block--title  →  "Maison  5 pièce(s)  2 chambre(s)  150 m²"
            (pièces / chambres / surface habitable parsés depuis ce texte libellé ;
             le bloc .item__options non libellé est ambigu → ignoré)
  - Prix  : .item__price  →  "420 000 €"
  - Photos: picture.media-js img[src]  (//freeimmo.staticlbi.com/...)
  - Réf   : id numérique du slug détail final, ou data-add-url=...idbien=NNNN

Couverture : réseau national à implantation très inégale. Sur la zone Val-de-Loire
             le stock est faible (45 a quelques biens ; 72/28/89 souvent 0).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.tower-immobilier.fr"
MAX_PAGES = 30          # garde-fou ; ~21 pages observées
PHOTOS_PER_CARD = 10


# Types de bien (segment d'URL / 1er mot du titre) à conserver
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|maison-de-village",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()
    by_dept: dict[str, int] = {d: 0 for d in departements}

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/vente/{page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Tower] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("article.item")
            if not cards:
                break

            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                cp = bien["code_postal"]
                # POST-FILTRE DÉPARTEMENT STRICT (pas de filtre serveur)
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

                bien["departement"] = cp[:2]
                seen_ids.add(aid)
                results.append(bien)
                by_dept[cp[:2]] += 1

            await asyncio.sleep(0.5)

    for d in sorted(departements):
        print(f"[Tower] Dept {d}: {by_dept.get(d, 0)} annonces")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a.links-group__link") or card.find("a", href=True)
    href = link.get("href", "") if link else ""
    if not href or "/vente/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien : segment {type} de l'URL /vente/{city}/{type}/{id-slug}
    parts = [p for p in href.split("/") if p]
    type_seg = parts[2] if len(parts) > 2 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean) and not _KEEP_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        return None
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # id_annonce : idbien du bouton sélection, sinon id numérique du slug détail
    id_annonce = ""
    btn = card.select_one("[data-add-url]")
    if btn:
        m = re.search(r"idbien=(\d+)", btn.get("data-add-url", ""))
        if m:
            id_annonce = m.group(1)
    if not id_annonce and parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_annonce = m.group(1)
    id_annonce = id_annonce or url

    # Localisation : "Ville (CODEPOSTAL)"
    city_el = card.select_one(".item__block--city")
    loc = city_el.get_text(" ", strip=True) if city_el else ""
    ville, code_postal = _parse_loc(loc)

    # Détail libellé : "Maison  5 pièce(s)  2 chambre(s)  150 m²"
    detail_el = card.select_one(".item__block--title") or card.select_one(".item__title")
    detail_text = detail_el.get_text(" ", strip=True) if detail_el else ""
    detail_text = re.sub(r"\s+", " ", detail_text)

    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", detail_text)
    chambres = _parse_int(r"(\d+)\s*chambre", detail_text)
    surface = _parse_surface(detail_text)

    titre = f"{type_bien.title()} {ville}".strip()

    # Prix
    price_el = card.select_one(".item__price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Photos
    photos: list[str] = []
    for img in card.select("picture.media-js img, .item__media-swiper img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "tower_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Tower Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Saint-Saud-Lacoussière (24470)' → ('Saint-Saud-Lacoussière', '24470')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_surface(text: str) -> float | None:
    """Dernier nombre suivi de 'm²' dans le détail → surface habitable."""
    matches = re.findall(r"(\d[\d\s\xa0]*)\s*m²", text)
    if not matches:
        return None
    val = re.sub(r"[\s\xa0]", "", matches[-1])
    try:
        f = float(val)
        if 8 <= f <= 5000:
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
    print(f"\nTotal Tower Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p/{b.get('chambres') or '?'}ch"
            f" — {b['type_bien']} — {b['ville']}"
        )

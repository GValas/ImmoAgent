"""scrapers/weelodge.py — Weelodge (réseau de mandataires, ~200 conseillers)

Méthode : scrape_simple (httpx) — SSR pur (Symfony + Stimulus/UX-Live).
          Le HTML brut contient déjà toutes les cartes (pas de Cloudflare bloquant ;
          le serveur répond en HTTP 422 mais sert la page complète — on l'accepte).

URL pattern : /recherche?offerType=sell&propertyType=house&page={N}
              → recherche NATIONALE, pas de filtre département serveur exploitable
              → POST-FILTRE strict sur code_postal[:2] (comme remax / noovimo).
              ~468 biens vente répartis sur ~20 pages (24 cartes/page).

Cartes : a.card-body, imbriquée dans un wrapper div.card (qui porte les <img>).
  - URL    : a.card-body[href] → /recherche/vm{ID}-maison-a-vendre-{pieces}-pieces-de-{m2}-m2-a-{ville-slug}
  - Prix   : .list_details-left .list_price → "380 000 €"
  - Type   : .list_details-left p (2e) → "Maison"
  - Loc    : .list_details-left p (3e) → "Gagny 93220"  (ville + CP)
  - Droite : .list_details-right p → [surface habitable m², chambres, terrain m²]
             (icônes : fa-ruler-combined = surface, fa-bed = chambres, fa-tree-deciduous = terrain)
  - Pièces : dans le slug d'URL ("...-{N}-pieces-...")
  - Photos : <img> du div.card parent (media/cache/.../property_image/*.webp)
  - id     : "vm{ID}" extrait du slug d'URL.

Couverture : réseau national ; implantation inégale → sur les départements cibles
             le stock peut être faible voire nul selon les runs.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.weelodge.fr"
SEARCH_PATH = "/recherche?offerType=sell&propertyType=house"
MAX_PAGES = 25          # ~20 pages réelles ; marge de sécurité
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()
    by_dept: dict[str, int] = {}

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}{SEARCH_PATH}&page={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Weelodge] Erreur page {page}: {e}")
                break

            # Le serveur renvoie 422 mais sert le HTML complet ; on n'accepte
            # que 200/422 (tout autre code → page invalide).
            if r.status_code not in (200, 422):
                print(f"[Weelodge] Page {page}: HTTP {r.status_code}, arrêt")
                break

            cards = BeautifulSoup(r.text, "html.parser").select("a.card-body")
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

                # POST-FILTRE DÉPARTEMENT STRICT (0 fuite hors-zone)
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
                bien["departement"] = cp[:2]
                results.append(bien)
                by_dept[cp[:2]] = by_dept.get(cp[:2], 0) + 1
                new_on_page += 1

            await asyncio.sleep(0.55)

    for dept in sorted(by_dept):
        print(f"[Weelodge] Dept {dept}: {by_dept[dept]} annonces")
    print(f"[Weelodge] Total retenu (zone): {len(results)}")
    return results


def _parse_card(card) -> dict | None:
    href = card.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce : "vm{ID}" dans le slug
    m_id = re.search(r"(vm\d+)", href)
    id_annonce = m_id.group(1) if m_id else url

    # Bloc gauche : prix / type / "Ville CP"
    left = card.select_one(".list_details-left")
    left_ps = [p.get_text(" ", strip=True) for p in left.select("p")] if left else []

    price_el = card.select_one(".list_price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    type_txt = left_ps[1] if len(left_ps) > 1 else "Maison"
    type_bien = type_txt.lower().strip() or "maison"

    loc_txt = left_ps[2] if len(left_ps) > 2 else (left_ps[-1] if left_ps else "")
    ville, code_postal = _parse_loc(loc_txt)

    # Bloc droit : surface habitable / chambres / terrain
    right_ps = (
        [p.get_text(" ", strip=True) for p in card.select(".list_details-right p")]
    )
    surface = _first_m2(right_ps, 0)
    chambres = _first_int(right_ps, 1)
    surface_terrain = _first_m2(right_ps, 2)

    # Pièces : depuis le slug "...-{N}-pieces-..."
    pieces = None
    m_p = re.search(r"-(\d+)-pieces", href)
    if m_p:
        pieces = int(m_p.group(1))

    # Surface de secours depuis le slug "...-de-{m2}-m2-..."
    if surface is None:
        m_s = re.search(r"-de-([\d.,]+)-m2", href)
        if m_s:
            surface = _to_float(m_s.group(1))

    # Titre reconstruit
    titre = f"{type_txt} {pieces or ''} pièces {ville}".replace("  ", " ").strip()

    # Photos : dans le wrapper div.card parent
    photos: list[str] = []
    wrapper = card.find_parent("div", class_="card") or card.parent
    if wrapper:
        for img in wrapper.select("img"):
            src = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-lazy-src")
                or ""
            )
            if src and not src.startswith("data:"):
                if src.startswith("//"):
                    src = "https:" + src
                elif src.startswith("/"):
                    src = BASE_URL + src
                photos.append(src)
    # dédup en gardant l'ordre
    photos = list(dict.fromkeys(photos))[:PHOTOS_PER_CARD]

    return {
        "source": "weelodge",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Weelodge",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Gagny 93220' → ('Gagny', '93220')."""
    cp = ""
    m = re.search(r"(\d{5})", text)
    if m:
        cp = m.group(1)
    ville = re.sub(r"\s*\d{5}\s*$", "", text).strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0 ]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _to_float(raw: str) -> float | None:
    raw = re.sub(r"[\s\xa0 ]", "", raw).replace(",", ".")
    # garde un seul point décimal
    if raw.count(".") > 1:
        raw = raw.replace(".", "", raw.count(".") - 1)
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _first_m2(items: list[str], idx: int) -> float | None:
    """Extrait une valeur en m² du texte à l'index donné (ex '144 m²')."""
    if idx >= len(items):
        return None
    m = re.search(r"([\d\s\xa0 .,]+)\s*m", items[idx])
    return _to_float(m.group(1)) if m else None


def _first_int(items: list[str], idx: int) -> int | None:
    if idx >= len(items):
        return None
    m = re.search(r"(\d+)", items[idx])
    return int(m.group(1)) if m else None


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
    print(f"\nTotal Weelodge: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['pieces'] or '?'}p — {b['ville']}"
            f" — {len(b['photos'])} photos"
        )

"""scrapers/agencecim.py — Agence CIM (réseau 3 agences Occitanie)

Site : https://www.agencecim.fr  (Toulouse / Montpellier / Montauban)
Méthode : scrape_simple (httpx) — SSR HTML (CMS twimmopro, nginx).

URL pattern (pages de liste, une seule page, pas de pagination) :
  /vente-maison-en-haute-garonne.html          → Haute-Garonne (31)
  /vente-appartement-en-haute-garonne.html
  /vente-maison-en-tarn-et-garonne.html        → Tarn-et-Garonne (82)
  /vente-appartement-en-tarn-et-garonne.html
  /vente-maison-dans-le-languedoc-roussillon.html    → région (34/30/11/66 mélangés !)
  /vente-appartement-dans-le-languedoc-roussillon.html

Filtre département : il n'y a PAS de code postal dans les cartes de liste, et la
page "languedoc-roussillon" MÉLANGE plusieurs départements (34 Hérault, 30 Gard,
11 Aude, 66 P.-O.). On ne peut donc PAS se fier à l'URL seule.
→ Stratégie : on lit les `data-latgps`/`data-longgps` de chaque carte et on
   applique un POST-FILTRE STRICT par bounding box départementale (DEPT_BBOX).
   Toute carte dont les coordonnées tombent hors de la box du département cible
   est rejetée → 0 fuite hors-département.

Cartes : article.listing-thumbnail
  - data-lien        : URL relative de la fiche
  - data-content-name: référence (ex "1-932V591M")  → id_annonce
  - data-prix        : "749 000 €"
  - data-title       : "6 pièces 190 m²"  → pièces + surface habitable
  - data-details     : ville (HTML, ex "Toulouse<br/>")
  - data-photo       : vignette
  - data-latgps / data-longgps : coordonnées (post-filtre dept)
  - chambres : extraites du slug d'URL (".../vente-maison-5-chambres-...")

Couverture : Occitanie uniquement (31, 34, 82). Hors zone Val-de-Loire/Ouest
             actuelle → renverra 0 bien sur 72/28/45/89 (normal, pas un bug).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.agencecim.fr"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Département → pages de liste à scraper (relatives à BASE_URL).
# Le département réel est garanti par le POST-FILTRE GPS (DEPT_BBOX), pas par l'URL :
# la page "languedoc-roussillon" mélange 34/30/11/66, on ne garde que la box ciblée.
DEPT_PAGES: dict[str, list[str]] = {
    "31": [
        "/vente-maison-en-haute-garonne.html",
        "/vente-appartement-en-haute-garonne.html",
    ],
    "82": [
        "/vente-maison-en-tarn-et-garonne.html",
        "/vente-appartement-en-tarn-et-garonne.html",
    ],
    "34": [
        "/vente-maison-dans-le-languedoc-roussillon.html",
        "/vente-appartement-dans-le-languedoc-roussillon.html",
    ],
}

# Bounding box (lat_min, lat_max, lon_min, lon_max) par département.
# Post-filtre STRICT : une carte hors box est rejetée (0 fuite hors-zone).
DEPT_BBOX: dict[str, tuple[float, float, float, float]] = {
    "31": (42.70, 43.95, 0.40, 2.10),   # Haute-Garonne
    "82": (43.80, 44.45, 0.75, 1.95),   # Tarn-et-Garonne
    "34": (43.20, 44.00, 2.55, 4.20),   # Hérault
}


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for dept in departements:
            pages = DEPT_PAGES.get(dept)
            if not pages:
                continue  # département non couvert par CIM (Occitanie only)
            try:
                biens = await _scrape_dept(
                    client, dept, pages, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[AgenceCIM] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[AgenceCIM] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    pages: list[str],
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()
    bbox = DEPT_BBOX.get(dept)

    for path in pages:
        url = BASE_URL + path
        r = await client.get(url)
        if r.status_code != 200:
            continue

        cards = BeautifulSoup(r.text, "html.parser").select("article.listing-thumbnail")
        for card in cards:
            try:
                bien = _parse_card(card, dept)
            except Exception:
                continue
            if not bien:
                continue

            # ── POST-FILTRE DÉPARTEMENT STRICT (par GPS) ──
            lat, lon = bien.pop("_lat", None), bien.pop("_lon", None)
            if bbox is None:
                continue
            if lat is None or lon is None:
                # pas de coords fiables → on n'accepte pas (évite toute fuite)
                continue
            lat_min, lat_max, lon_min, lon_max = bbox
            if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
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

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    href = card.get("data-lien") or ""
    if not href:
        link = card.select_one("a[href]")
        href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    ref = (card.get("data-content-name") or "").strip()
    id_annonce = ref or url

    # Ville depuis data-details (HTML, ex "Toulouse<br/>" ou "Muret Campagne<br/>")
    det = card.get("data-details", "")
    ville = BeautifulSoup(det, "html.parser").get_text(" ", strip=True)
    ville = re.sub(r"\s+", " ", ville).strip()

    # Type de bien depuis le slug d'URL
    type_bien = "maison" if "vente-maison" in href else (
        "appartement" if "vente-appartement" in href else "bien"
    )

    # data-title : "6 pièces 190 m²"
    data_title = card.get("data-title", "")
    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", data_title)
    surface = _parse_surface(data_title)

    # chambres depuis le slug d'URL ("...5-chambres...")
    chambres = _parse_int(r"(\d+)-chambres?", href)

    # Titre lisible
    titre_parts = [type_bien.capitalize(), ville]
    if data_title:
        titre_parts.append(data_title.replace("\xa0", " ").strip())
    titre = " ".join(p for p in titre_parts if p).strip()

    # Description (extrait dans la carte)
    desc_el = card.select_one(".liste-item-desc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    prix = _parse_price(card.get("data-prix", ""))

    # Photo
    photos = []
    photo = card.get("data-photo") or ""
    if photo and not photo.startswith("data:"):
        photos.append(photo)
    img = card.select_one("img.img-responsive")
    if img:
        src = img.get("src") or ""
        if src and not src.startswith("data:") and src not in photos:
            photos.append(src)

    # Coordonnées (pour le post-filtre dept) — extraites puis retirées avant retour
    lat = _to_float(card.get("data-latgps"))
    lon = _to_float(card.get("data-longgps"))

    return {
        "source": "agencecim",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",  # absent des cartes de liste (présent seulement sur la fiche)
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Agence CIM",
        "_lat": lat,
        "_lon": lon,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text or "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_surface(text: str) -> float | None:
    """'6 pièces 190 m²' → 190.0"""
    m = re.search(r"([\d\s\xa0]+)\s*m²", text or "")
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 5 <= f <= 5000:
                return f
        except ValueError:
            pass
    return None


def _to_float(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


# ── CLI standalone ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()

    def _run(depts, label):
        biens = asyncio.run(
            search(
                {
                    "departements": depts,
                    "prix_max": criteres.prix_max,
                    "prix_min": getattr(criteres, "prix_min", 0),
                    "surface_min": criteres.surface_min,
                }
            )
        )
        print(f"\n=== {label} — Total Agence CIM: {len(biens)} annonces")
        # Pas de CP dans les cartes → on affiche les départements ciblés (filtre GPS)
        depts_vus = sorted({b["departement"] for b in biens})
        print(f"Départements vus : {depts_vus}")
        for b in biens[:10]:
            print(
                f"  [{b['departement']}] {b['titre'][:55]}"
                f" — {b['prix']}€"
                f" — {b.get('surface') or '?'}m²"
                f" — {b.get('pieces') or '?'}p"
                f" — {b['ville']}"
            )
        return biens

    # 1) Départements de test (criteria.md) : hors zone CIM → attendu 0
    _run(criteres.departements, f"criteria.md {criteres.departements}")
    # 2) Départements natifs CIM : preuve que parsing + filtre GPS marchent, 0 fuite
    _run(["31", "82", "34"], "natifs CIM [31, 82, 34]")

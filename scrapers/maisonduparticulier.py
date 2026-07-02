"""scrapers/maisonduparticulier.py — Maison Du Particulier (P2P, particulier à particulier)

Méthode : scrape_simple (httpx) — SSR HTML (moteur PHP « Script PAG »).
Site d'annonces immobilières GRATUIT de particulier à particulier (sans agence,
sans commission). Petit inventaire national, mais réel et bien réparti.

URL pattern : /annonces/offres/Vente-Maison/{NomDepartement}?page=N
  → filtre département CÔTÉ SERVEUR via le nom de département (slug littéral, ex.
    « Indre », « Loir-et-Cher », « Nievre » sans accent). Vérifié : 0 fuite — chaque
    code postal renvoyé commence bien par le code du département demandé.

Cartes : a.background-ads-listing  (dans div.background-ads-listing-container)
  - URL      : href de la carte  → /annonce/{Region}-{Departement}-Vente-Maison-{slug}-{id}
  - Titre    : p.title-listing
  - DPE      : span.energy (texte = lettre A..G)
  - Surface  : span.area    → "116m²"
  - Pièces   : span.rooms   → "4 pièces"
  - Loc      : p.localisation-listing > span (1er = dept, 2e = "62470 Camblain-Châtelain")
  - Prix     : span.price-listing → "130 000,00 €"
  - Photos   : div.bloc-listing-last strong (nb) ; vignette principale dans .bloc-listing-picture img
  - id       : data-id du a.icon-heart frère, ou nombre final de l'URL

Catégorie scrapée : Vente-Maison uniquement (maisons / propriétés de particuliers).
Couverture observée (zone cible, 2026-06) : 18(4) 49(3) 58(3) 89(2) 36(1) 37(1)
45(1) ; 72/28/41/53 = 0 stock. Volume faible mais 100 % P2P et 0 fuite dept.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://maisonduparticulier.fr"
MAX_PAGES = 5
PHOTOS_PER_CARD = 1  # la liste n'expose que la vignette ; gallery.py enrichira


# Code département → nom de département tel qu'utilisé dans l'URL (sans accents)
DEPT_SLUGS: dict[str, str] = {
    "72": "Sarthe",
    "28": "Eure-et-Loir",
    "45": "Loiret",
    "89": "Yonne",
    "49": "Maine-et-Loire",
    "37": "Indre-et-Loire",
    "36": "Indre",
    "18": "Cher",
    "58": "Nievre",
    "41": "Loir-et-Cher",
    "53": "Mayenne",
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
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, slug, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[MaisonDuParticulier] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[MaisonDuParticulier] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    slug: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/annonces/offres/Vente-Maison/{slug}?page={page}"
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select("a.background-ads-listing")
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            try:
                bien = _parse_card(card, dept)
            except Exception:
                continue
            if not bien:
                continue

            # Sécurité ANTI-FUITE : on n'accepte que le département cible.
            # Filtre serveur déjà OK, mais on re-vérifie le préfixe du code postal.
            if bien["code_postal"] and bien["code_postal"][:2] != dept:
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
            new_on_page += 1

        if new_on_page == 0:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    href = card.get("href", "")
    if not href or "/annonce/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce : data-id du a.icon-heart frère, sinon nombre final de l'URL
    id_annonce = ""
    container = card.parent
    heart = container.select_one("a.icon-heart[data-id]") if container else None
    if heart:
        id_annonce = heart.get("data-id", "")
    if not id_annonce:
        m = re.search(r"-(\d+)/?$", href)
        id_annonce = m.group(1) if m else url

    # Titre
    title_el = card.select_one("p.title-listing")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    # DPE (lettre A..G)
    dpe = None
    dpe_el = card.select_one("span.energy")
    if dpe_el:
        t = dpe_el.get_text(strip=True).upper()
        if t in {"A", "B", "C", "D", "E", "F", "G"}:
            dpe = t

    # Surface habitable
    area_el = card.select_one("span.area")
    surface = _parse_surface(area_el.get_text(strip=True) if area_el else "")

    # Pièces
    rooms_el = card.select_one("span.rooms")
    pieces = _parse_int(r"(\d+)", rooms_el.get_text(strip=True) if rooms_el else "")

    # Localisation : span[0] = département, span[1] = "62470 Camblain-Châtelain"
    ville, code_postal = "", ""
    loc_el = card.select_one("p.localisation-listing")
    if loc_el:
        spans = loc_el.select("span")
        for sp in spans:
            txt = sp.get_text(" ", strip=True)
            if "price-listing" in (sp.get("class") or []):
                continue
            m = re.search(r"(\d{5})\s+(.+)", txt)
            if m:
                code_postal = m.group(1)
                ville = m.group(2).strip()
                break
    # Secours ville depuis le titre si la liste ne donne rien
    if not ville and titre:
        ville = titre.split("–")[-1].strip()[:80]

    # Prix
    price_el = card.select_one("span.price-listing")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Photos : vignette principale (galerie complète récupérée plus tard par gallery.py)
    photos = []
    img = card.select_one(".bloc-listing-picture img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    if not titre:
        titre = f"Maison {ville}".strip()

    return {
        "source": "maisonduparticulier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": None,  # P2P : pas d'agence
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_surface(text: str) -> float | None:
    """'116m²' → 116.0"""
    m = re.search(r"(\d[\d\s\xa0]*)\s*m", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 5000:
                return f
        except ValueError:
            pass
    return None


def _parse_price(text: str) -> float | None:
    """'130 000,00 €' → 130000.0"""
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    # virgule décimale française : on coupe la partie décimale
    cleaned = re.sub(r",\d{1,2}$", "", cleaned)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text)
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
    print(f"\nTotal Maison Du Particulier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — DPE {b.get('dpe') or '?'}"
            f" — {b['type_bien']} — {b['ville']}"
        )

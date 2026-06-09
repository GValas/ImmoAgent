"""scrapers/occitanie_immo.py — Occitanie-Immo (100% entre particuliers)

Méthode : scrape_simple (httpx) — SSR HTML (nginx, ~45 ko, pas de Cloudflare/CSR).
Portail RÉGIONAL : couvre UNIQUEMENT les 13 départements d'Occitanie
(09, 11, 12, 30, 31, 32, 34, 46, 48, 65, 66, 81, 82). Hors de cette liste,
le site n'a aucun stock → la recherche renvoie [] (et 0 fuite).

Filtre département CÔTÉ SERVEUR via le formulaire de recherche (POST /).
  1. GET / → récupère le token de session anti-CSRF `a10t04h` (input hidden).
  2. POST / (form-urlencoded) avec :
       a10t04h={token}, type_bien=Maison|Appartement, depa={NN}, machin=''
     (prix_min/prix_max/surf_min/surf_max optionnels)
     → renvoie les annonces du département choisi.
  3. Pagination : POST /?page={N} avec les mêmes données de formulaire
     (le JS `goToPage` ne fait que poser action='?page=N' puis submit).

Cartes : div.realty-item
  - URL    : a[href*="id="]  → /?id={ID}   (id_annonce = ID numérique)
  - Loc    : .realty-item-place  →  "81150 Marssac-sur-Tarn"  (CP + ville)
  - Titre  : .realty-item-title
  - Prix   : .realty-item-price  →  "260 000 €"
  - Surface: .realty-item-surface  →  "95 m²"
  - Texte  : .realty-item-description
  - Photo  : .realty-item-image  → style background-image: url(...)

Type de bien : filtré côté serveur (type_bien=Maison) ; on ne demande que les
maisons (le projet cible maisons/propriétés). Re-vérifie le type via le formulaire.

Post-filtre département STRICT : `code_postal[:2] == dept` (objectif 0 fuite),
même si le filtre serveur est déjà fiable (vérifié dept 81 : aucune fuite).

Particularité : P2P (pas d'agence). agence = None.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.occitanie-immo.fr"
MAX_PAGES = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Départements RÉELLEMENT couverts par le portail (Occitanie uniquement).
# Hors de cette liste : aucun stock → on n'interroge même pas le serveur.
DEPT_DISPO: set[str] = {
    "09", "11", "12", "30", "31", "32", "34", "46", "48", "65", "66", "81", "82",
}


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # On ne garde que les départements couverts par le portail.
    cibles = [d for d in departements if d in DEPT_DISPO]
    if not cibles:
        print(
            "[OccitanieImmo] Aucun département cible en Occitanie "
            f"({sorted(DEPT_DISPO)}) → 0 annonce."
        )
        return []

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        token = await _get_token(client)
        if not token:
            print("[OccitanieImmo] Token de session introuvable → abandon.")
            return []

        for dept in cibles:
            try:
                biens = await _scrape_dept(
                    client, token, dept, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[OccitanieImmo] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[OccitanieImmo] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _get_token(client: httpx.AsyncClient) -> str | None:
    r = await client.get(BASE_URL + "/")
    if r.status_code != 200:
        return None
    inp = BeautifulSoup(r.text, "html.parser").find("input", {"name": "a10t04h"})
    return inp.get("value") if inp else None


async def _scrape_dept(
    client: httpx.AsyncClient,
    token: str,
    dept: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    form = {
        "a10t04h": token,
        "type_bien": "Maison",
        "depa": dept,
        "prix_min": "",
        "prix_max": "",
        "surf_min": "",
        "surf_max": "",
        "machin": "",
    }

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/?page={page}"
        r = await client.post(url, data=form)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select("div.realty-item")
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

            # Post-filtre STRICT : 0 fuite hors-département.
            if not bien["code_postal"] or bien["code_postal"][:2] != dept:
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
    link = card.select_one('a[href*="id="]')
    href = link.get("href", "") if link else ""
    m_id = re.search(r"id=(\d+)", href)
    if not m_id:
        return None
    id_annonce = m_id.group(1)
    url = f"{BASE_URL}/?id={id_annonce}"

    # Localisation : "81150 Marssac-sur-Tarn"
    place_el = card.select_one(".realty-item-place")
    place = place_el.get_text(" ", strip=True) if place_el else ""
    code_postal, ville = _parse_place(place)

    title_el = card.select_one(".realty-item-title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    desc_el = card.select_one(".realty-item-description")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    price_el = card.select_one(".realty-item-price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    surf_el = card.select_one(".realty-item-surface")
    surface = _parse_surface(surf_el.get_text(" ", strip=True) if surf_el else "")

    if not titre:
        titre = f"Maison {ville}".strip()

    # Photo : background-image dans le style de .realty-item-image
    photos: list[str] = []
    img_el = card.select_one(".realty-item-image")
    if img_el and img_el.get("style"):
        m = re.search(r"url\(([^)]+)\)", img_el["style"])
        if m:
            src = m.group(1).strip("'\"")
            if src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)

    return {
        "source": "occitanie_immo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": None,  # 100% entre particuliers
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_place(text: str) -> tuple[str, str]:
    """'81150 Marssac-sur-Tarn' → ('81150', 'Marssac-sur-Tarn')."""
    m = re.match(r"\s*(\d{5})\s+(.*)$", text)
    if m:
        return m.group(1), m.group(2).strip()
    # Pas de CP en tête : tente n'importe où
    m2 = re.search(r"(\d{5})", text)
    cp = m2.group(1) if m2 else ""
    ville = re.sub(r"\d{5}", "", text).strip()
    return cp, ville


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text.replace("\xa0", ""))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    m = re.search(r"([\d\s\xa0]+)\s*m", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
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
    print(f"\nTotal Occitanie-Immo: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

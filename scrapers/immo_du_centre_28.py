"""scrapers/immo_du_centre_28.py — Immo du Centre (Chartres, Eure-et-Loir)

Agence locale d'Eure-et-Loir (membre du réseau Immo de France, mais sur son
propre domaine immoducentre28.fr — source distincte ; la déduplication du
hunter fusionnera tout recoupement). Implantation exclusivement sur le 28
(Chartres et environs), donc l'inventaire est 100 % département 28.

Méthode : scrape_simple (httpx) — SSR HTML (CMS Webgenery/Apimo), pas de JS.
URL pattern (liste maisons) :
    page 1 : /fr/acheter/maison
    page N : /fr/acheter/maison/all/all/all/all/all/all/{N}
On scrape aussi /fr/acheter/propriete pour les « propriétés ».
Filtre département : le site est mono-département (28) ; post-filtre STRICT sur
code_postal[:2] == "28" en sécurité. On NE scrape que si 28 est demandé
(aucune URL par département ailleurs : agence locale).

Cartes : article.minifiche_liste[data-uuid]
  - Lien détail : .photo a[href]  → /fr/vente/{slug}/{UUID}
  - Photo       : .photo img[src]
  - Localisation : .titreMiniFiche → "Ville - CODEPOSTAL"
  - Accroche     : .accroche
  - Prix         : .prix → "Prix de vente : 139 000 €"
  - JSON-LD <script application/ld+json> : name ("Vente maison N pièces S m² à
    Ville (CP)"), description complète, offers.price, image → surface/pièces.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.immoducentre28.fr"
MAX_PAGES = 10
PHOTOS_PER_CARD = 6
# Ce scraper ne couvre que le département 28 (agence locale Chartres).
DEPT = "28"
# Catégories du site à scraper (maisons + propriétés)
LIST_PATHS = ["/fr/acheter/maison", "/fr/acheter/propriete"]


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    # Agence mono-département : ne tourne que si le 28 est ciblé.
    if DEPT not in departements:
        print(f"[ImmoCentre28] Dept {DEPT} non demandé — skip")
        return []

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for path in LIST_PATHS:
            try:
                biens = await _scrape_path(
                    client, path, prix_max, prix_min, surface_min, seen_ids
                )
                results.extend(biens)
                print(f"[ImmoCentre28] {path}: {len(biens)} annonces")
            except Exception as e:
                print(f"[ImmoCentre28] Erreur {path}: {e}")
            await asyncio.sleep(0.5)

    print(f"[ImmoCentre28] Dept {DEPT}: {len(results)} annonces")
    return results


async def _scrape_path(
    client: httpx.AsyncClient,
    path: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
    seen_ids: set[str],
) -> list[dict]:
    biens: list[dict] = []

    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = f"{BASE_URL}{path}"
        else:
            url = f"{BASE_URL}{path}/all/all/all/all/all/all/{page}"

        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select(
            "article.minifiche_liste"
        )
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

            # Post-filtre dept STRICT (mono-département, double sécurité)
            if bien["code_postal"] and bien["code_postal"][:2] != DEPT:
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

        # La pagination « clampe » sur la dernière page (répète les mêmes UUID)
        # → si aucun bien nouveau, on arrête.
        if new_on_page == 0:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card) -> dict | None:
    uuid = card.get("data-uuid") or ""

    link = card.select_one(".photo a[href]") or card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    id_annonce = uuid or url

    # JSON-LD (riche) : name, description, offers.price, image
    name = ""
    description = ""
    prix = None
    photos: list[str] = []
    sc = card.select_one('script[type="application/ld+json"]')
    if sc and sc.string:
        try:
            data = json.loads(sc.string)
            graph = data.get("@graph") or [data]
            node = graph[0] if graph else {}
            name = node.get("name", "") or ""
            description = node.get("description", "") or ""
            offers = node.get("offers") or {}
            if isinstance(offers, dict):
                prix = _to_float(offers.get("price"))
            img = node.get("image")
            if isinstance(img, str):
                photos = [img]
            elif isinstance(img, list):
                photos = [i for i in img if isinstance(i, str)]
        except Exception:
            pass

    # Localisation : .titreMiniFiche → "Ville - CODEPOSTAL"
    titre_el = card.select_one(".titreMiniFiche")
    loc_txt = titre_el.get_text(" ", strip=True) if titre_el else ""
    ville, code_postal = _parse_loc(loc_txt, name)

    # Type de bien depuis le name JSON-LD ("Vente maison ..." / "Vente propriété ...")
    type_bien = "maison"
    if re.search(r"propri[ée]t[ée]", name, re.IGNORECASE):
        type_bien = "propriete"

    # Surface / pièces depuis le name : "N pièces S m²"
    pieces = None
    m_p = re.search(r"(\d+)\s*pi[eè]ces?", name, re.IGNORECASE)
    if m_p:
        pieces = int(m_p.group(1))
    surface = None
    m_s = re.search(r"(\d[\d\s\xa0]*)\s*m²", name)
    if m_s:
        surface = _to_float(re.sub(r"[\s\xa0]", "", m_s.group(1)))

    # Prix de secours depuis .prix si JSON-LD muet
    if prix is None:
        prix_el = card.select_one(".prix")
        if prix_el:
            prix = _parse_price(prix_el.get_text(" ", strip=True))

    # Accroche en secours de description
    if not description:
        acc = card.select_one(".accroche")
        description = acc.get_text(" ", strip=True) if acc else ""

    # Photos de secours depuis l'<img> de la carte
    if not photos:
        img = card.select_one(".photo img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src and not src.startswith("data:"):
                photos = [src]
    photos = photos[:PHOTOS_PER_CARD]

    titre = name or (f"{type_bien.title()} {ville}".strip())

    return {
        "source": "immo_du_centre_28",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": DEPT,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,   # parfois dans la description → gallery.py
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Immo du Centre (Chartres)",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(loc_txt: str, name: str) -> tuple[str, str]:
    """'Le Gault-Saint-Denis - 28800' → ('Le Gault-Saint-Denis', '28800').

    Secours sur le name JSON-LD : '... à Ville (28800)'.
    """
    cp = ""
    ville = ""
    m_cp = re.search(r"(\d{5})", loc_txt)
    if m_cp:
        cp = m_cp.group(1)
        ville = re.sub(r"\s*-?\s*\d{5}.*$", "", loc_txt).strip(" -")
    if not cp and name:
        m = re.search(r"à\s+(.+?)\s*\((\d{5})\)", name)
        if m:
            ville, cp = m.group(1).strip(), m.group(2)
    if not ville and name:
        m = re.search(r"à\s+(.+?)\s*\(", name)
        if m:
            ville = m.group(1).strip()
    return ville, cp


def _to_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "."))
    except (ValueError, TypeError):
        return None


def _parse_price(text: str) -> float | None:
    """'Prix de vente : 139 000 €' → 139000.0"""
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
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
    print(f"\nTotal Immo du Centre 28: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

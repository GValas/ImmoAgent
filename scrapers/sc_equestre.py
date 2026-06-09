"""scrapers/sc_equestre.py — SC Immobilier Equestre (sc-equestre.fr)

Méthode : scrape_simple (httpx) — SSR HTML WordPress (WPML).

Agence MONO-ENSEIGNE spécialisée immobilier équestre en NOUVELLE-AQUITAINE
(propriétés équestres, haras, écuries, centres équestres avec terrain). Inventaire
très faible (~9 propriétés + page terrains souvent vide) et géographiquement
concentré sur la Gironde / Dordogne / Charente(-Maritime) / Lot-et-Garonne
(depts 33, 24, 16, 17, 47). Aucune implantation dans la zone Val-de-Loire/Ouest.

Listing  : https://www.sc-equestre.fr/nos-proprietes-equestres-a-vendre/
           (pas de pagination observée ; page unique ~9 cartes)
           + /nos-terrains-equestres-a-vendre/ (terrains équestres, souvent 0)

PAS de filtre département par URL → on scrape le listing complet puis on
POST-FILTRE sur le département présent dans le bloc localisation de la carte.

Cartes : div.bien
  - URL/titre : h2 a[href]   → /nos-proprietes-equestres-a-vendre/{slug}/
  - id        : div.fav[data-id]
  - photo     : a.img-wrap img[src]
  - etiquette : .etiquette  (SOUS-COMPROMIS / VENDU / EXCLUSIVITE...)
  - blocs     : .c-principale  →  3 lignes :
                  1) "VILLE - DD Departement"        (DD = code département)
                  2) "Surface du terrain : N.NN ha"
                  3) "199 000 €"

Pas de code postal sur la carte : le département est donné directement (DD).
→ Filtre dept = comparaison stricte sur ce DD (et non sur code_postal[:2]).
  code_postal laissé à None (non publié sur la carte).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.sc-equestre.fr"
LISTING_PATHS = [
    "/nos-proprietes-equestres-a-vendre/",
    "/nos-terrains-equestres-a-vendre/",
]
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Type de bien : la page propriétés = propriétés équestres ; la page terrains = terrains.
_TYPE_BY_PATH = {
    "/nos-proprietes-equestres-a-vendre/": "propriété équestre",
    "/nos-terrains-equestres-a-vendre/": "terrain équestre",
}


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for path in LISTING_PATHS:
            try:
                biens = await _scrape_listing(client, path)
            except Exception as e:
                print(f"[SCEquestre] Erreur listing {path}: {e}")
                continue

            kept = 0
            for bien in biens:
                # Filtre département STRICT (dept dans la carte, pas de CP)
                if bien["departement"] not in departements:
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
                results.append(bien)
                kept += 1

            print(f"[SCEquestre] {path}: {len(biens)} cartes, {kept} retenues (zone)")
            await asyncio.sleep(0.5)

    return results


async def _scrape_listing(client: httpx.AsyncClient, path: str) -> list[dict]:
    r = await client.get(BASE_URL + path)
    if r.status_code != 200:
        return []

    type_bien = _TYPE_BY_PATH.get(path, "propriété équestre")
    cards = BeautifulSoup(r.text, "html.parser").select("div.bien")
    biens: list[dict] = []
    for card in cards:
        try:
            bien = _parse_card(card, type_bien)
        except Exception:
            continue
        if bien:
            biens.append(bien)
    return biens


def _parse_card(card, type_bien: str) -> dict | None:
    link = card.select_one("h2 a[href]") or card.select_one("a.img-wrap[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce : data-id du bouton favori, sinon slug d'URL
    fav = card.select_one(".fav[data-id]")
    id_annonce = fav.get("data-id") if fav else ""
    if not id_annonce:
        id_annonce = url.rstrip("/").rsplit("/", 1)[-1]

    # Titre
    h2 = card.select_one("h2")
    titre = h2.get_text(" ", strip=True) if h2 else ""

    # Blocs .c-principale : [localisation, surface terrain, prix]
    blocs = [c.get_text(" ", strip=True) for c in card.select(".c-principale")]
    loc_txt = blocs[0] if blocs else ""
    ville, departement = _parse_loc(loc_txt)

    surface_terrain = None
    prix = None
    for b in blocs[1:]:
        if surface_terrain is None:
            t = _parse_terrain_ha(b)
            if t is not None:
                surface_terrain = t
                continue
        if prix is None:
            p = _parse_price(b)
            if p is not None:
                prix = p

    if prix is None:
        # secours : n'importe quel "... €" dans la carte
        prix = _parse_price(card.get_text(" ", strip=True))

    # Etiquette (statut) — informatif, ajouté à la description
    etq = ""
    for e in card.select(".etiquette"):
        txt = e.get_text(strip=True)
        if txt:
            etq = txt
            break

    # Photo
    photos = []
    img = card.select_one("a.img-wrap img") or card.select_one("img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    description = titre
    if etq:
        description = f"[{etq}] {titre}"

    return {
        "source": "sc_equestre",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": departement,
        "ville": ville[:80],
        "code_postal": None,  # non publié sur la carte
        "surface": None,      # surface habitable absente du listing
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "SC Immobilier Equestre",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'MONTGUYON - 17 Charente Maritime' → ('Montguyon', '17')."""
    ville = ""
    dept = ""
    m = re.match(r"\s*(.+?)\s*-\s*(\d{1,3})\b", text)
    if m:
        ville = m.group(1).strip().title()
        dept = m.group(2).zfill(2)[:2]
    else:
        # pas de séparateur : tente d'isoler un code dept en tête de la partie droite
        m2 = re.search(r"\b(\d{2})\b", text)
        dept = m2.group(1) if m2 else ""
        ville = text.strip().title()
    return ville, dept


def _parse_terrain_ha(text: str) -> float | None:
    """'Surface du terrain : 10.36 ha' → 103600.0 (m²)."""
    m = re.search(r"([\d]+(?:[.,]\d+)?)\s*ha", text, re.IGNORECASE)
    if m:
        try:
            return round(float(m.group(1).replace(",", ".")) * 10000, 1)
        except ValueError:
            return None
    # repli : valeur en m²
    m2 = re.search(r"([\d\s\xa0]+)\s*m[²2]", text, re.IGNORECASE)
    if m2:
        val = re.sub(r"[\s\xa0]", "", m2.group(1))
        try:
            return float(val)
        except ValueError:
            return None
    return None


def _parse_price(text: str) -> float | None:
    m = re.search(r"([\d][\d\s\xa0]{2,})\s*€", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        return float(val)
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
    print(f"\nTotal SC Immobilier Equestre: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

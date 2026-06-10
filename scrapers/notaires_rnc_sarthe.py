"""scrapers/notaires_rnc_sarthe.py — RNC Notaires (SELAS Réseau Notaires & Conseils, Arnage 72)

Méthode : scrape_simple (httpx) — SSR HTML (template Genapi/immonot, pas de JS).
Site     : https://www.rnc.notaires.fr — étude notariale unique en Sarthe ;
           portefeuille mono-département (72), ~137 annonces.

URL pattern (liste paginée) : /fr_FR/3/{page}/annonces-immobilieres.html
  - 9 cartes par page, ~16 pages.
  - La page régionale /annonces-immobilieres-sarthe.html pointe vers la même liste.

Cartes : div.bloc-annonce-carre  (à l'intérieur d'un <a href=/annonces/detail/...>)
  - URL    : a[href]  → /annonces/detail/{id}__{w...}/key/3/vente-{type}-{dept}-{ville}.html
  - 2ᵉ container-fluid → 3 .row :
       row0 : "Vente Maison"            +  "372 000 €"        (type + prix)
      row1 : "La Suze-sur-Sarthe"       +  honoraires de négo (ville en .light-color)
      row2 : "200.0 m²"                 +  "Réf. HB-1862"     (surface + référence)
  - Photo  : img[src] (relatif → BASE_URL)

Filtre département : les cartes ne portent PAS de code postal, mais le SLUG du
  département figure TOUJOURS dans l'URL détail (ex: ...-sarthe-...). On en déduit
  le département via DEPT_SLUGS (vérifié : 137/137 URLs matchées, dominante 72).
  → 0 fuite hors-zone garantie (on rejette toute carte sans slug dept cible).
  Pas de code postal disponible en liste → code_postal = None, departement renseigné.

Types conservés : maisons / propriétés / fermes… (appartements, terrains, parkings,
  locaux exclus).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.rnc.notaires.fr"
MAX_PAGES = 20
PHOTOS_PER_CARD = 1  # la liste n'expose qu'une vignette par carte

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Slug département présent dans l'URL détail → code département.
# (trié par longueur décroissante au moment du match pour éviter les sous-chaînes)
DEPT_SLUGS: dict[str, str] = {
    "sarthe": "72",
    "eure-et-loir": "28",
    "loiret": "45",
    "yonne": "89",
    "maine-et-loire": "49",
    "indre-et-loire": "37",
    "loir-et-cher": "41",
    "mayenne": "53",
    "nievre": "58",
    "indre": "36",
    "cher": "18",
}

# Types de bien (libellé carte) à conserver : maisons / propriétés / fermes...
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|long[èe]re|manoir|ch[âa]teau|moulin|"
    r"demeure|domaine|mas|g[îi]te|corps de ferme|maison de village|pavillon",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"viager|location",
    re.IGNORECASE,
)


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
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/fr_FR/3/{page}/annonces-immobilieres.html"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[RNC] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                "div.bloc-annonce-carre"
            )
            if not cards:
                break

            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # Filtre département STRICT (déduit du slug URL — 0 fuite)
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

            await asyncio.sleep(0.5)

    print(f"[RNC] Total : {len(results)} annonces")
    return results


def _dept_from_url(href: str) -> str | None:
    """Déduit le code département du slug présent dans l'URL détail."""
    for slug in sorted(DEPT_SLUGS, key=len, reverse=True):
        if re.search(r"-" + re.escape(slug) + r"-", href):
            return DEPT_SLUGS[slug]
    return None


def _parse_card(card) -> dict | None:
    link = card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href or "/annonces/detail/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    dept = _dept_from_url(href)
    if not dept:
        return None  # pas de département cible identifiable → on écarte (0 fuite)

    # Conteneur texte = 2ᵉ .container-fluid (le 1er contient l'image)
    containers = card.select(".container-fluid")
    text_box = containers[1] if len(containers) > 1 else card
    rows = text_box.select(".row")

    # row0 : type + prix
    type_label = ""
    prix = None
    if len(rows) > 0:
        cols = rows[0].select("div")
        if cols:
            type_label = cols[0].get_text(" ", strip=True)
        prix = _parse_price(rows[0].get_text(" ", strip=True))
    type_clean = re.sub(r"^vente\s+", "", type_label, flags=re.IGNORECASE).strip()

    if _EXCLUDE_TYPE.search(type_clean) and not _KEEP_TYPE.search(type_clean):
        return None
    if not _KEEP_TYPE.search(type_clean):
        return None
    type_bien = type_clean or "maison"

    # row1 : ville (.light-color en début) + honoraires (description)
    ville = ""
    description = ""
    if len(rows) > 1:
        loc_el = rows[1].select_one(".light-color")
        if loc_el:
            ville = loc_el.get_text(" ", strip=True)
        description = rows[1].get_text(" ", strip=True)
    # Nettoie une éventuelle parenthèse de commune déléguée : "Ballon-Saint-Mars (Ballon)"
    ville_clean = re.sub(r"\s*\([^)]*\)\s*$", "", ville).strip()

    # row2 : surface + référence
    surface = None
    ref = ""
    if len(rows) > 2:
        row2_txt = rows[2].get_text(" ", strip=True)
        surface = _parse_surface(row2_txt)
        m_ref = re.search(r"R[ée]f\.?\s*([\w./-]+)", row2_txt)
        if m_ref:
            ref = m_ref.group(1)

    # id_annonce : référence sinon segment id de l'URL
    id_annonce = ref
    if not id_annonce:
        m_id = re.search(r"/detail/(\w+)", href)
        id_annonce = m_id.group(1) if m_id else url

    titre = f"{type_bien} {ville_clean}".strip()

    # Photo (vignette)
    photos = []
    img = card.select_one("img[src]")
    if img:
        src = img.get("src", "")
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "notaires_rnc_sarthe",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien.lower(),
        "description": description[:1200],
        "departement": dept,
        "ville": ville_clean[:80],
        "code_postal": "",  # absent en liste
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "RNC Notaires (Arnage)",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    """'Vente Maison 372 000 €' → 372000.0 (1er montant en €)."""
    m = re.search(r"([\d][\d\s\xa0]*)\s*€", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
        return f if f > 1000 else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'200.0 m²' → 200.0"""
    m = re.search(r"([\d]+(?:[.,]\d+)?)\s*m²", text)
    if m:
        try:
            f = float(m.group(1).replace(",", "."))
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
    print(f"\nTotal RNC Notaires : {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

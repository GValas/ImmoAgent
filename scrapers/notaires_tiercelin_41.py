"""scrapers/notaires_tiercelin_41.py — Étude notariale TIERCELIN-BRUNET-DUVIVIER
(Montrichard Val de Cher), portail Genapi/notaires.fr.

Méthode : scrape_simple (httpx) — SSR HTML (cartes rendues côté serveur).
URL pattern (pagination Genapi) :
    /fr_FR/3/{page}/annonces-immobilieres.html   (9 annonces/page, ~5 pages)
    page d'accueil équivalente : /annonces-immobilieres-loir-et-cher.html

Couverture : étude mono-implantation couvrant le Loir-et-Cher (41) et
l'Indre-et-Loire (37) — deux départements cibles. Inventaire global ~44 biens,
toutes localisations dans 41/37 (vérifié : 0 fuite hors-zone).

Cartes : div.ns-property-card (enveloppées dans un <a> vers la page détail)
  - URL    : <a href=".../annonces/detail/{id}__{wXXXX}/key/3/vente-{type}-{dept-slug}-{ville}.html">
  - Type + département + ville : déduits du SLUG de l'URL détail
             (ex. "vente-maison-loir-et-cher-chisseaux" → maison / 41 / chisseaux)
  - Prix   : .c__price b  →  "356 217,21 €" (FAI ; le HT est dans data-tipso)
  - Loc    : .c__location  →  nom de commune (PAS de code postal dans la liste)
  - Type   : .c__type span →  "Vente Maison"
  - Infos  : .c__quickinfos →  "145.0 m 2 1360 m2 6 p 4 chb"
             (1er "m 2" = surface habitable, "NNNN m2" = terrain si présent,
              "N p" = pièces, "N chb" = chambres)
  - Photos : background-image: url(/photoProduit/...jpg)

Filtre département : le portail ne sert que 41 + 37. Le département est extrait
du slug de l'URL détail (le code postal n'est pas dans la vue liste) puis
re-vérifié STRICTEMENT contre la liste des départements cibles → 0 fuite.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.tiercelin-brunet-duvivier.notaires.fr"
LIST_URL = BASE_URL + "/fr_FR/3/{page}/annonces-immobilieres.html"
MAX_PAGES = 12
PHOTOS_PER_CARD = 10
AGENCE = "Notaires Tiercelin-Brunet-Duvivier (Montrichard)"


# Nom de département (slug d'URL) → code. Seuls 41 et 37 sont servis par l'étude,
# mais on garde une table générale pour rester robuste.
DEPT_NAME_TO_CODE: dict[str, str] = {
    "sarthe": "72",
    "eure-et-loir": "28",
    "loiret": "45",
    "yonne": "89",
    "maine-et-loire": "49",
    "indre-et-loire": "37",
    "indre": "36",
    "cher": "18",
    "nievre": "58",
    "loir-et-cher": "41",
    "mayenne": "53",
}

# Types de bien à conserver : maisons / propriétés / demeures
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|murs|viager",
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
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = LIST_URL.format(page=page)
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[TiercelinNotaires] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.ns-property-card")
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

                # Filtre département STRICT (0 fuite) : on n'accepte que les
                # départements cibles, déduits du slug d'URL détail.
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
                new_on_page += 1

            if new_on_page == 0 and page > 1:
                # plus de nouveaux biens conservés sur cette page : on continue
                # encore une page (les types exclus peuvent saturer une page),
                # mais on arrête si la page est vide de cartes (géré plus haut).
                pass

            await asyncio.sleep(0.5)

    print(f"[TiercelinNotaires] Total: {len(results)} annonces")
    return results


def _parse_card(card) -> dict | None:
    # Lien détail : <a> parent direct, ou premier lien interne
    link = card.find_parent("a") or card.select_one("a[href*='/annonces/detail/']")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Slug : .../key/3/vente-{type}-{dept-slug}-{ville}.html
    m = re.search(r"/key/\d+/(.+?)\.html", href)
    slug = m.group(1) if m else ""
    type_bien, dept_code, ville = _parse_slug(slug)
    if dept_code is None:
        return None

    # Filtrage de type (on ne garde que maisons / propriétés)
    if _EXCLUDE_TYPE.search(type_bien) and not _KEEP_TYPE.search(type_bien):
        return None
    if not _KEEP_TYPE.search(type_bien):
        return None
    type_label = type_bien.replace("-", " ").strip() or "maison"

    # id_annonce : token wXXXX du segment détail
    m_id = re.search(r"/detail/[^/]*?(w\d+)", href)
    id_annonce = m_id.group(1) if m_id else url

    # Prix
    price_el = card.select_one(".c__price b") or card.select_one(".c__price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Ville (depuis la carte ; secours = slug)
    loc_el = card.select_one(".c__location")
    ville_card = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville_final = ville_card or ville

    # Type affiché
    type_el = card.select_one(".c__type span")
    type_aff = type_el.get_text(" ", strip=True) if type_el else ""

    # Quickinfos : "145.0 m 2 1360 m2 6 p 4 chb"
    qi_el = card.select_one(".c__quickinfos")
    qi_text = qi_el.get_text(" ", strip=True) if qi_el else ""
    surface, surface_terrain, pieces, chambres = _parse_quickinfos(qi_text)

    # Titre
    titre = f"{type_aff} {ville_final}".strip() or type_label.title()

    # Photos
    photos = []
    for src in re.findall(r"background-image:\s*url\(([^)]+)\)", str(card)):
        src = src.strip("'\" ")
        if not src or src.startswith("data:"):
            continue
        if src.startswith("/"):
            src = BASE_URL + src
        photos.append(src)
    # dédoublonne en gardant l'ordre
    seen = set()
    photos = [p for p in photos if not (p in seen or seen.add(p))][:PHOTOS_PER_CARD]

    return {
        "source": "notaires_tiercelin_41",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_label,
        "description": "",
        "departement": dept_code,
        "ville": ville_final[:80],
        "code_postal": "",
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": AGENCE,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_slug(slug: str) -> tuple[str, str | None, str]:
    """'vente-maison-loir-et-cher-chisseaux' → ('maison', '41', 'chisseaux').

    On reconnaît le nom de département (clé la plus longue qui matche) au milieu
    du slug ; le type est ce qui précède 'vente-', la ville ce qui suit le dept.
    """
    if not slug.startswith("vente-"):
        return "", None, ""
    rest = slug[len("vente-"):]
    for name in sorted(DEPT_NAME_TO_CODE, key=len, reverse=True):
        marker = "-" + name + "-"
        idx = rest.find(marker)
        if idx >= 0:
            type_part = rest[:idx]
            ville_part = rest[idx + len(marker):]
            ville = ville_part.replace("-", " ").strip().title()
            return type_part, DEPT_NAME_TO_CODE[name], ville
        # cas où le dept est en fin de slug (pas de ville) — peu probable
        if rest.endswith("-" + name):
            type_part = rest[: -(len(name) + 1)]
            return type_part, DEPT_NAME_TO_CODE[name], ""
    return rest, None, ""


def _parse_price(text: str) -> float | None:
    """'356 217,21 €' → 356217.21"""
    if not text:
        return None
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    # virgule décimale française
    cleaned = cleaned.replace(",", ".")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    # si plusieurs points (séparateurs de milliers résiduels), garder le dernier
    if cleaned.count(".") > 1:
        parts = cleaned.split(".")
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_quickinfos(text: str) -> tuple[float | None, float | None, int | None, int | None]:
    """'145.0 m 2 1360 m2 6 p 4 chb' → (145.0, 1360.0, 6, 4).

    1er nombre avant 'm 2'/'m2' = surface habitable (peut être décimal),
    nombre entier suivi de 'm2' = terrain (optionnel),
    'N p' = pièces, 'N chb' = chambres.
    """
    surface = None
    terrain = None
    pieces = None
    chambres = None

    # Surface habitable : premier "X(.Y) m 2" / "X m2"
    m_hab = re.search(r"(\d+(?:\.\d+)?)\s*m\s*2?\b", text)
    if m_hab:
        try:
            f = float(m_hab.group(1))
            if 5 <= f <= 5000:
                surface = f
        except ValueError:
            pass

    # Terrain : un "NNNN m2" (entier) après la surface habitable.
    m2_vals = re.findall(r"(\d+(?:\.\d+)?)\s*m\s*2?\b", text)
    if len(m2_vals) >= 2:
        try:
            terrain = float(m2_vals[1])
        except ValueError:
            terrain = None

    m_p = re.search(r"(\d+)\s*p\b", text)
    if m_p:
        pieces = int(m_p.group(1))
    m_chb = re.search(r"(\d+)\s*chb\b", text)
    if m_chb:
        chambres = int(m_chb.group(1))

    return surface, terrain, pieces, chambres


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
    print(f"\nTotal Tiercelin Notaires: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

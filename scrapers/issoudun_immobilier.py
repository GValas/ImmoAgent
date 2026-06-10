"""scrapers/issoudun_immobilier.py — Issoudun Immobilier (agence locale, Indre)

Méthode : scrape_simple (httpx) — SSR HTML (CMS « Spot My Place » / Alveen).
Site : https://www.issoudun-immobilier.fr
URL liste : /tous-les-biens            (page 1)
            /tous-les-biens?page={N}    (pages suivantes)
            → la liste complète des annonces est rendue dans le HTML brut (statique),
              aucune exécution JS nécessaire.

Cartes : <a> wrappant un <figcaption>
  - URL/ref : a[href] → /achat/{ville}-{type}...,{ref}   (ex: ...,681)
  - Titre   : .bititre
  - Ville   : .biville           (nom de commune en MAJUSCULES)
  - Prix    : .prix              → "139 000€"
  - Réf     : .ref               → "REF.681"
  - Photos  : div.img-diapo[data-src] / div.img-diapo-list[data-src]
La carte est dupliquée (vue grille + vue liste masquée) → dédup par ref.

Filtre département : la liste de la carte ne contient PAS de code postal, seulement
le nom de commune. Le site est une agence mono-secteur (Issoudun / Châteauroux),
qui n'opère QUE sur l'Indre (36) et le Cher (18) — tous deux départements cibles.
Le formulaire de recherche du site expose la liste exhaustive « COMMUNE-CP » de
toutes les communes ayant des biens (balises <option value="ISSOUDUN-36100">…) :
on en construit un mapping commune→code_postal, qui sert de filtre département FIABLE.
Toute commune inconnue est rejetée (0 fuite garantie). Post-filtre strict cp[:2].

Caractéristiques (surface, surface_terrain, pièces, chambres) : non présentes dans
la liste → extraites best-effort depuis le titre. Pas de requête détail (le worker
gallery.py enrichira en page détail au besoin).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.issoudun-immobilier.fr"
LIST_PATH = "/tous-les-biens"
MAX_PAGES = 9
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien (segment d'URL / titre) à conserver : maisons / propriétés.
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps de ferme|"
    r"maison de village|grange|pavillon|fermette|batisse|bâtisse",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"hangar|entrepot|entrepôt",
    re.IGNORECASE,
)


def _build_cp_map(html: str) -> dict[str, str]:
    """Construit {COMMUNE: code_postal} depuis les <option value="COMMUNE-CP">.

    C'est la liste officielle des communes du secteur de l'agence (toutes dans
    les départements 36 et 18). Sert de filtre département fiable.
    """
    cp_map: dict[str, str] = {}
    for ville, cp in re.findall(r'<option\s+value="(.+?)-(\d{5})">', html):
        cp_map[_norm_ville(ville)] = cp
    return cp_map


def _norm_ville(v: str) -> str:
    return re.sub(r"\s+", " ", v.strip().upper())


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_refs: set[str] = set()
    cp_map: dict[str, str] = {}

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = BASE_URL + LIST_PATH + ("" if page == 1 else f"?page={page}")
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Issoudun] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            if not cp_map:
                cp_map = _build_cp_map(r.text)

            cards = _extract_cards(r.text)
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card, cp_map, departements)
                except Exception:
                    continue
                if not bien:
                    continue
                if bien["id_annonce"] in seen_refs:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                seen_refs.add(bien["id_annonce"])
                results.append(bien)
                new_on_page += 1

            # Le site recycle la page 1 quand on dépasse le nombre réel de pages
            # → aucune nouvelle annonce ⇒ on arrête.
            if new_on_page == 0:
                break

            await asyncio.sleep(0.5)

    print(f"[Issoudun] {len(results)} annonces (depts cibles)")
    return results


def _extract_cards(html: str) -> list:
    """Renvoie les <figcaption> uniques (la vue grille suffit ; on dédup par ref)."""
    soup = BeautifulSoup(html, "html.parser")
    return soup.select("figcaption")


def _parse_card(card, cp_map: dict[str, str], departements: set[str]) -> dict | None:
    a = card.find_parent("a")
    href = a.get("href", "") if a else ""
    if not href or "/achat/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Référence : segment final après la virgule (...,681) ou .ref "REF.681"
    ref = ""
    m_ref = re.search(r",(\d+)\s*$", href)
    if m_ref:
        ref = m_ref.group(1)
    if not ref:
        ref_el = card.select_one(".ref")
        if ref_el:
            m = re.search(r"(\d+)", ref_el.get_text(strip=True))
            if m:
                ref = m.group(1)
    if not ref:
        return None
    id_annonce = f"issoudun-{ref}"

    # Ville → code postal via le mapping officiel du site (filtre département)
    biv_el = card.select_one(".biville")
    ville_raw = biv_el.get_text(strip=True) if biv_el else ""
    ville_key = _norm_ville(ville_raw)
    code_postal = cp_map.get(ville_key, "")
    if not code_postal:
        # Commune inconnue du mapping → on ne peut pas garantir le département → exclu
        return None
    dept = code_postal[:2]
    if departements and dept not in departements:
        return None

    # Titre
    title_el = card.select_one(".bititre")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre and a and a.get("title"):
        titre = a.get("title")
    if not titre:
        titre = ville_raw.title()

    # Type de bien depuis le titre / segment d'URL
    type_src = f"{titre} {href}"
    if _EXCLUDE_TYPE.search(type_src) and not _KEEP_TYPE.search(type_src):
        return None
    if not _KEEP_TYPE.search(type_src):
        return None
    type_bien = _guess_type(titre)

    # Prix
    price_el = card.select_one(".prix")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Surface / terrain / pièces best-effort depuis le titre
    surface = _parse_surface_hab(titre)
    surface_terrain = _parse_terrain(titre)
    pieces = _parse_pieces(titre)
    chambres = _parse_chambres(titre)

    # Photos
    photos = []
    scope = a if a else card
    for div in scope.select("[data-src]"):
        src = div.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    # dédoublonne en gardant l'ordre
    photos = list(dict.fromkeys(photos))[:PHOTOS_PER_CARD]

    return {
        "source": "issoudun_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": dept,
        "ville": ville_raw.title()[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Issoudun Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _guess_type(titre: str) -> str:
    t = titre.lower()
    for kw in (
        "longère", "longere", "manoir", "château", "chateau", "moulin",
        "ferme", "fermette", "propriété", "propriete", "grange", "pavillon",
        "villa", "maison de village", "maison",
    ):
        if kw in t:
            return kw.replace("è", "e").replace("é", "e")
    return "maison"


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text).replace("&euro;", "")
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface_hab(text: str) -> float | None:
    """Cherche une surface habitable plausible dans le titre."""
    if not text:
        return None
    for m in re.finditer(r"(\d[\d\s\xa0]*)\s*m[²2]", text, re.IGNORECASE):
        ctx = text[max(0, m.start() - 25): m.start()].lower()
        # exclut les mentions de terrain
        if "terrain" in ctx or "jardin" in ctx or "parcelle" in ctx:
            continue
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 2000:
                return f
        except ValueError:
            continue
    return None


def _parse_terrain(text: str) -> float | None:
    if not text:
        return None
    m = re.search(
        r"terrain[^0-9]{0,30}?([\d\s\xa0]+)\s*m[²2]", text, re.IGNORECASE
    )
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            return float(val)
        except ValueError:
            pass
    return None


def _parse_pieces(text: str) -> int | None:
    m = re.search(r"(\d+)\s*pi[eè]ces?", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"\bT\s?(\d+)\b", text)
    return int(m.group(1)) if m else None


def _parse_chambres(text: str) -> int | None:
    m = re.search(r"(\d+)\s*chambres?", text, re.IGNORECASE)
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
    print(f"\nTotal Issoudun Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

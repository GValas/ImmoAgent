"""scrapers/retz_immobilier.py — Cabinet Retz-Immobilier (Demeures & Caractère)

Méthode : scrape_simple (httpx) — SSR HTML (site builder IONOS MyWebsite).

Agence indépendante à inventaire LOCAL : demeures de caractère, manoirs, longères,
propriétés équestres/agricoles sur la façade Atlantique. Couverture réelle :
44 (Loire-Atlantique), 85 (Vendée), 56 (Morbihan), + limitrophes 79 / 17.
Aucun stock dans les départements cibles du projet (72/28/45/89/49/37/36/18/58/41/53).

Structure du site :
  - PAS de portail/listing paginé classique ni de filtre département serveur.
  - Une page-mère "maisons-propriétés-à-vendre-…" qui n'expose que des liens vers
    des sous-pages de catégories (tranches de prix + thématiques).
  - Les biens sont des blocs de texte rédigés à la main : chaque bien = un <h2>
    (`<h2><span class="diyfeDecoration">…</span></h2>`) décrivant le bien, souvent
    préfixé par le département `NN - …`, suivi d'un bloc `module-type-textWithImage`
    contenant le lien fiche `a.imagewrapper[href=".../{ID}-slug/"][title="…"]`.
  - Les tranches de prix (300-000, de-300-000-à-600-000, de-600-000-à-900-000,
    900-000) PARTITIONNENT tout le catalogue ; les pages thématiques sont des
    sous-ensembles. On crawle toutes les catégories connues et on DÉDUPLIQUE par ID.

Filtre département : AUCUN côté serveur → POST-FILTRE Python.
  Le département est extrait, en cascade, depuis :
    1. le préfixe `NN -` / `NN –` / `NNNNN -` en tête de titre/h2,
    2. tout code postal à 5 chiffres présent dans le titre,
    3. un mapping ville/secteur → département (le site cite surtout des villes).
  Les biens dont le département reste indéterminé sont écartés (pas de fuite).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import urllib.parse

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.retz-immobilier.com"
# Page-mère du listing (URL avec accents → encodée à l'usage)
LISTING_PATH = (
    "/maisons-propriétés-à-vendre-vendée-loire-atlantique-morbihan-"
    "et-départements-limitrophes/"
)

# Sous-pages catégories : les tranches de prix partitionnent le catalogue,
# les thématiques ajoutent d'éventuels biens manquants. Dédup par ID.
_CATEGORY_SLUGS = [
    "",  # page-mère elle-même
    "300-000/",
    "de-300-000-à-600-000/",
    "de-600-000-à-900-000/",
    "900-000/",
    "châteaux-manoirs-prestige/",
    "propriétés-equestres-agricoles/",
    "propriétés-avec-des-hectares/",
    "investissements-terrains/",
    "biens-pour-projets-commerciaux/",
    "bois-etang-pêcherie-chasse/",
    "idéales-pour-gîte-chambre-d-hôte/",
    "les-biens-au-bord-de-l-eau/",
    "les-biens-avec-piscine/",
    "les-biens-avec-tennis/",
    "les-biens-sur-parcours-de-golf/",
    "lofts-contemporaines/",
    "biens-avec-accès-mobilité-réduite/",
]

PHOTOS_PER_CARD = 3


# Villes / secteurs fréquemment cités → département (uniquement zone de l'agence).
_VILLE_DEPT = {
    # 44 Loire-Atlantique
    "nantes": "44", "clisson": "44", "vallet": "44", "boussay": "44",
    "ancenis": "44", "pornic": "44", "prefailles": "44", "préfailles": "44",
    "saint-brevin": "44", "st brevin": "44", "saint-herblain": "44",
    "la baule": "44", "guerande": "44", "guérande": "44", "piriac": "44",
    "pays de retz": "44", "saint-nazaire": "44", "la chapelle": "44",
    # 85 Vendée
    "challans": "85", "les herbiers": "85", "puy du fou": "85",
    "fontenay le comte": "85", "fontenay-le-comte": "85", "vendée": "85",
    "vendee": "85",
    # 56 Morbihan
    "vannes": "56", "ploermel": "56", "ploërmel": "56", "baden": "56",
    "morbihan": "56", "golfe du morbihan": "56",
    # 79 Deux-Sèvres / 17 Charente-Maritime (limitrophes)
    "la rochelle": "17", "marans": "17", "deux-sèvres": "79",
}

# Mots-clés titre → type de bien (exclut les biens non résidentiels)
_EXCLUDE_KEYWORDS = re.compile(
    r"\brestaurant\b|\bmurs\b|local commercial|fonds de commerce|\bbureau\b|"
    r"\bappartement\b|\bstudio\b",
    re.IGNORECASE,
)
_TYPE_MAP = [
    (re.compile(r"château", re.IGNORECASE), "château"),
    (re.compile(r"manoir|logis", re.IGNORECASE), "manoir"),
    (re.compile(r"chaumière|chaumiere|longère|longere", re.IGNORECASE), "longère"),
    (re.compile(r"propriété équestre|équestre|equestre", re.IGNORECASE), "propriété équestre"),
    (re.compile(r"corps de ferme|ferme", re.IGNORECASE), "ferme"),
    (re.compile(r"villa", re.IGNORECASE), "villa"),
    (re.compile(r"demeure|propriété|propriete", re.IGNORECASE), "propriété"),
    (re.compile(r"maison", re.IGNORECASE), "maison"),
]


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    raw = await _fetch_all_properties()

    results: list[dict] = []
    seen: set[str] = set()
    for bien in raw:
        if bien["id_annonce"] in seen:
            continue

        # POST-FILTRE département (aucun filtre serveur).
        dept = bien.get("departement") or ""
        if departements and dept not in departements:
            continue

        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        seen.add(bien["id_annonce"])
        results.append(bien)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[RetzImmobilier] Dept {dept}: {n} annonces")

    return results


async def _fetch_all_properties() -> list[dict]:
    biens_by_id: dict[str, dict] = {}
    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for slug in _CATEGORY_SLUGS:
            url = BASE_URL + urllib.parse.quote(LISTING_PATH + slug, safe=":/")
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    continue
            except Exception as e:
                print(f"[RetzImmobilier] Erreur page {slug or '[main]'}: {e}")
                continue

            for bien in _parse_page(r.text):
                biens_by_id.setdefault(bien["id_annonce"], bien)

            await asyncio.sleep(0.4)

    return list(biens_by_id.values())


def _parse_page(html: str) -> list[dict]:
    """Extrait les biens d'une page catégorie.

    Chaque bien = un lien fiche `a.imagewrapper[href=".../{ID}-…/"][title=…]`,
    le titre riche étant dans l'attribut `title`. On rattache le <h2> précédent
    (qui porte souvent le préfixe département) quand on peut.
    """
    soup = BeautifulSoup(html, "html.parser")
    biens: list[dict] = []

    for a in soup.select('a.imagewrapper[href]'):
        href = a.get("href", "")
        m_id = re.search(r"/(\d{5,6})-", href)
        if not m_id:
            continue
        pid = m_id.group(1)
        url = href if href.startswith("http") else BASE_URL + href

        # Titre riche : attribut title du lien (sinon alt de l'image)
        title = a.get("title") or ""
        if not title:
            img = a.find("img")
            if img:
                title = img.get("alt", "") or ""
        title = re.sub(r"\s+", " ", title).strip()

        # Header h2 le plus proche en amont (porte souvent "NN - …")
        h2_text = _nearest_header(a)

        bien = _build_bien(pid, url, title, h2_text)
        if bien:
            biens.append(bien)

    return biens


def _nearest_header(node) -> str:
    """Remonte le DOM pour trouver le <h2> de bien le plus proche en amont."""
    cur = node
    hops = 0
    while cur is not None and hops < 12:
        prev = cur.find_previous("h2")
        if prev:
            txt = prev.get_text(" ", strip=True)
            if len(txt) > 15:
                return re.sub(r"\s+", " ", txt).strip()
            cur = prev
        else:
            break
        hops += 1
    return ""


def _build_bien(pid: str, url: str, title: str, h2_text: str) -> dict | None:
    text = f"{h2_text} || {title}".strip()
    if not text:
        text = title or h2_text

    if _EXCLUDE_KEYWORDS.search(text):
        return None

    dept, code_postal = _extract_dept(h2_text, title, url)

    ville = _extract_ville(title) or _extract_ville(h2_text)

    type_bien = "propriété"
    for rx, label in _TYPE_MAP:
        if rx.search(text):
            type_bien = label
            break

    prix = _extract_prix(text)
    surface = _extract_surface(text)
    surface_terrain = _extract_terrain(text)
    chambres = _extract_int(r"(\d+)\s*chambres?", text)
    pieces = _extract_int(r"(\d+)\s*pi[eè]ces?", text)

    titre = title or h2_text
    titre = titre[:150]

    return {
        "source": "retz_immobilier",
        "url": url,
        "id_annonce": pid,
        "titre": titre,
        "type_bien": type_bien,
        "description": (h2_text or title)[:1200],
        "departement": dept,
        "ville": (ville or "")[:80],
        "code_postal": code_postal or "",
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "dpe": None,
        "photos": [],
        "agence": "Cabinet Retz-Immobilier",
    }


# ── Extraction département (cascade : préfixe → CP → ville) ────────────────────

def _extract_dept(h2_text: str, title: str, url: str) -> tuple[str, str]:
    code_postal = ""
    # 1) préfixe explicite en tête de h2 : "44 -", "85 –", "85300 -"
    for src in (h2_text, title):
        m = re.match(r"^\s*(\d{2})(\d{3})?\s*[-–—]", src)
        if m:
            if m.group(2):
                code_postal = m.group(1) + m.group(2)
            return m.group(1), code_postal

    # 2) code postal 5 chiffres n'importe où dans h2 puis titre
    for src in (h2_text, title):
        m_cp = re.search(r"\b(\d{5})\b", src)
        if m_cp:
            cp = m_cp.group(1)
            return cp[:2], cp

    # 3) mapping ville/secteur → département
    blob = f"{h2_text} {title}".lower()
    for needle, dept in _VILLE_DEPT.items():
        if needle in blob:
            return dept, ""

    return "", ""


def _extract_ville(text: str) -> str | None:
    if not text:
        return None
    # Ville en MAJUSCULES (le site écrit les villes en capitales)
    m = re.search(r"\b([A-ZÉÈÀÂÎ][A-ZÉÈÀÂÎ' \-]{3,})\b", text)
    if m:
        v = m.group(1).strip(" -'")
        # éviter de capturer des mots génériques
        if v.upper() not in {"VENTE", "VENDU", "PROPRIETE", "PROPRIÉTÉ",
                             "MAISON", "DEMEURE", "VENDEE", "VENDÉE", "MORBIHAN",
                             "EXCEPTIONNEL", "RARE"} and len(v) <= 40:
            return v.title()
    return None


# ── Helpers numériques ────────────────────────────────────────────────────────

def _extract_prix(text: str) -> float | None:
    # "350 000 €", "1 250 000 €"
    for m in re.finditer(r"([\d][\d\s\xa0\.]{4,})\s*€", text):
        val = re.sub(r"[\s\xa0\.]", "", m.group(1))
        try:
            f = float(val)
            if 10000 <= f <= 50_000_000:
                return f
        except ValueError:
            continue
    return None


def _extract_surface(text: str) -> float | None:
    # "185m² hab", "420 m² habitables", "env. 340m²"
    m = re.search(
        r"(\d[\d\s\xa0]*)\s*m²?\s*(?:hab|habitable|de surface)", text, re.IGNORECASE
    )
    if not m:
        # premier "NNN m²" plausible comme surface habitable
        m = re.search(r"(\d{2,4})\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 20 <= f <= 3000:
                return f
        except ValueError:
            pass
    return None


def _extract_terrain(text: str) -> float | None:
    # hectares "5,6 hectares" / "9 hectares" → m²
    m = re.search(r"(\d+(?:[,\.]\d+)?)\s*hectares?", text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(",", ".")) * 10000
        except ValueError:
            pass
    # "terrain … 5 600 m²" / "sur 3 768 m²"
    m = re.search(r"(?:terrain|sur|parc)[^0-9]{0,12}([\d\s\xa0]{3,})\s*m²", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 100 <= f <= 2_000_000:
                return f
        except ValueError:
            pass
    return None


def _extract_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
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
    print(f"\nTotal Retz-Immobilier (depts cibles): {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}/{b['code_postal'] or '-----'}] {b['titre'][:55]}"
            f" — {b['prix']}€ — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

    # Contrôle de fuite : aucun bien hors départements cibles ne doit rester.
    cibles = {str(d).zfill(2) for d in criteres.departements}
    fuites = [b for b in biens if b["departement"] and b["departement"] not in cibles]
    print(f"\nContrôle fuite hors-dept : {len(fuites)} fuite(s)")
    for b in fuites[:10]:
        print(f"  FUITE [{b['departement']}] {b['titre'][:60]}")

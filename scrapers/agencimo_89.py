"""scrapers/agencimo_89.py — Agencimo (agence locale de Sens, Yonne 89, depuis 1999)

Méthode : scrape_simple (httpx) — SSR HTML
URL liste : /annonces-immobilieres-sens-.html  (l'accueil liste aussi des biens,
            mais cette page est la liste complète des annonces ; ~31 cartes).

Particularité département :
  Agence MONO-89 (Sens et communes alentour). La page NE contient PAS le code
  postal du bien : le seul "89100 Sens" présent est l'adresse de l'agence
  (6 Grande rue). On NE peut donc PAS lire un CP fiable depuis le HTML.
  → Stratégie filtre dept : on résout la VILLE de la carte (h2) vers un code
    postal 89 via le dict VILLE_CP (communes réelles du secteur Sens). Toute
    carte dont la ville n'est pas résolvable en 89 est ÉCARTÉE (0 fuite garantie).
    Les libellés vagues type "15 mn de sens" sont rattachés à Sens (89100).

Cartes (page liste) : div.annonce
  - Type  : h1                  (ex: "MAISON DE VILLAGE")
  - Ville : h2                  (ex: "PONT SUR YONNE")
  - Réf+Prix : h3               (ex: "Référence : 3795  -  54 000 €")
              → si "/Mois" présent → LOCATION → exclu
  - URL   : a[href] (annonce-immobiliere-...html)
  - Photo : .img_annonces img[src]
  - Texte : p (description courte)

On exclut les types non résidentiels (local, garage, terrain, immeuble, parking).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.agencimo.com"
LISTE_URL = f"{BASE_URL}/annonces-immobilieres-sens-.html"
PHOTOS_PER_CARD = 12


# Communes du secteur de Sens (Yonne 89) → code postal.
# Sert UNIQUEMENT à dériver un CP fiable (la page ne le donne pas) et à
# garantir 0 fuite hors-89. Clés normalisées (minuscule, sans accent, espaces simples).
VILLE_CP: dict[str, str] = {
    "sens": "89100",
    "paron": "89100",
    "saint clement": "89100",
    "saint-clement": "89100",
    "maillot": "89100",
    "rosoy": "89100",
    "gron": "89100",
    "saligny": "89100",
    "malay le grand": "89100",
    "malay le petit": "89100",
    "courtois sur yonne": "89100",
    "soucy": "89100",
    "veron": "89510",
    "etigny": "89510",
    "passy": "89510",
    "marsangy": "89500",
    "villeneuve sur yonne": "89500",
    "armeau": "89500",
    "egriselles le bocage": "89500",
    "egriselles-le-bocage": "89500",
    "subligny": "89100",
    "saint denis les sens": "89100",
    "saint-denis-les-sens": "89100",
    "saint denis": "89100",
    "saint martin du tertre": "89100",
    "pont sur yonne": "89140",
    "champigny": "89340",
    "champigny sur yonne": "89340",
    "villeblevin": "89340",
    "villethierry": "89140",
    "villeperrot": "89140",
    "evry": "89140",
    "michery": "89140",
    "foucheres": "89150",
    "fouchères": "89150",
    "savigny sur clairis": "89150",
    "vernoy": "89150",
    "domats": "89150",
    "chaumot": "89500",
    "thorigny sur oreuse": "89260",
    "serbonnes": "89140",
    "cuy": "89140",
    "vinneuf": "89140",
    "saint serotin": "89140",
    "saint-serotin": "89140",
    "nailly": "89100",
    "gisy les nobles": "89140",
}

# Mention vague rattachée à Sens (89100) : "X mn de sens", "proche sens", etc.
_SENS_AREA = re.compile(r"\bsens\b|\bde sens\b", re.IGNORECASE)

# Types non résidentiels à exclure (sur le h1 / l'URL)
_EXCLUDE_TYPE = re.compile(
    r"local|commercial|garage|parking|terrain|immeuble|bureau|fonds|fond de commerce",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    s = s.lower().strip()
    repl = (
        ("à", "a"), ("â", "a"), ("ä", "a"),
        ("é", "e"), ("è", "e"), ("ê", "e"), ("ë", "e"),
        ("î", "i"), ("ï", "i"), ("ô", "o"), ("ö", "o"),
        ("ù", "u"), ("û", "u"), ("ü", "u"), ("ç", "c"),
    )
    for a, b in repl:
        s = s.replace(a, b)
    s = re.sub(r"[’']", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _resolve_cp(ville_raw: str) -> tuple[str, str] | None:
    """Résout la ville d'une carte vers (ville_propre, code_postal 89).

    Retourne None si la ville n'est pas une commune 89 connue ET n'est pas
    une mention rattachable au secteur de Sens → la carte est écartée (0 fuite).
    """
    norm = _norm(ville_raw)
    if not norm:
        return None
    if norm in VILLE_CP:
        return ville_raw.title(), VILLE_CP[norm]
    # Mention vague ("15 mn de sens", "proche de sens") → Sens 89100
    if _SENS_AREA.search(norm):
        return ville_raw.strip(), "89100"
    return None


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Agence mono-89 : si 89 n'est pas demandé, rien à scraper.
    if "89" not in departements:
        print("[Agencimo] 89 hors zone demandée → 0 annonce")
        return []

    results: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        try:
            r = await client.get(LISTE_URL)
        except Exception as e:
            print(f"[Agencimo] Erreur réseau liste : {e}")
            return []
        if r.status_code != 200:
            print(f"[Agencimo] Liste HTTP {r.status_code}")
            return []

        cards = BeautifulSoup(r.text, "html.parser").select("div.annonce")
        print(f"[Agencimo] {len(cards)} cartes brutes sur la liste")

        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue

            # Filtre dept STRICT : CP dérivé doit être en 89 et demandé.
            cp = bien["code_postal"]
            if not cp or cp[:2] != "89" or "89" not in departements:
                continue

            aid = bien["id_annonce"]
            if aid in seen:
                continue

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            seen.add(aid)
            results.append(bien)

    print(f"[Agencimo] Dept 89 : {len(results)} annonces retenues")
    return results


def _parse_card(card) -> dict | None:
    # Type (h1)
    h1 = card.select_one("h1")
    type_raw = h1.get_text(" ", strip=True) if h1 else ""
    if not type_raw:
        return None
    if _EXCLUDE_TYPE.search(type_raw):
        return None

    # URL
    link = card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    # Le path porte le type ; la query string contient des noms de filtres
    # (garage=, parking=…) qu'il ne faut PAS confondre avec le type du bien.
    href_path = href.split("?", 1)[0]
    if _EXCLUDE_TYPE.search(href_path):
        return None
    url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('./')}"

    # Réf + prix + détection location (h3)
    h3 = card.select_one("h3")
    h3_text = h3.get_text(" ", strip=True) if h3 else ""
    if re.search(r"/\s*mois", h3_text, re.IGNORECASE):
        return None  # location

    ref = ""
    m_ref = re.search(r"R[ée]f[ée]rence\s*:\s*([^\-–]+?)\s*[-–]", h3_text)
    if m_ref:
        ref = m_ref.group(1).strip()
    prix = _parse_price(h3_text)

    # Ville (h2) → résolution dept
    h2 = card.select_one("h2")
    ville_raw = h2.get_text(" ", strip=True) if h2 else ""
    resolved = _resolve_cp(ville_raw)
    if not resolved:
        return None
    ville, code_postal = resolved

    # id_annonce : ref si dispo, sinon slug d'URL
    id_num = ""
    m_id = re.search(r"-([a-z0-9]+)\.html", href_path, re.IGNORECASE)
    if m_id:
        id_num = m_id.group(1)
    id_annonce = ref or id_num or url

    titre = type_raw.title()
    if ville:
        titre = f"{titre} {ville}"

    # Description courte (premier <p> textuel de la carte)
    description = ""
    for p in card.select("p"):
        txt = p.get_text(" ", strip=True)
        if txt and not re.search(r"R[ée]f[ée]rence", txt) and len(txt) > len(description):
            description = txt

    # Photo de carte
    photos = []
    for img in card.select(".img_annonces img, figure img, img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:"):
            continue
        if "/img/" in src.lower() or src.lower().endswith(".png"):
            continue  # icônes
        full = src if src.startswith("http") else f"{BASE_URL}/{src.lstrip('./')}"
        if full not in photos:
            photos.append(full)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "agencimo_89",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_raw.lower(),
        "description": description[:1200],
        "departement": "89",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": None,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Agencimo (Sens)",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    # On isole la portion après le tiret de référence si possible
    m = re.search(r"[-–]\s*([\d\s\xa0]+)\s*€", text)
    raw = m.group(1) if m else text
    cleaned = re.sub(r"[^\d]", "", raw)
    try:
        val = float(cleaned) if cleaned else None
    except ValueError:
        return None
    # Garde-fou : un prix de vente plausible
    if val is not None and val < 1000:
        return None
    return val


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
    print(f"\nTotal Agencimo: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:15]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b['type_bien']} — {b['ville']}"
            f" — {len(b['photos'])} photo(s)"
        )

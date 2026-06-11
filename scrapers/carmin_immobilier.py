"""scrapers/carmin_immobilier.py — Carmin Immobilier (agence locale, Bourges / Cher)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress, thème maison `carmin`).

Agence MONO-DÉPARTEMENT implantée à Bourges : tout son inventaire est dans le
Cher (18). Le scraper ne sert donc QUE si 18 fait partie des départements cibles ;
sinon il ne fait aucune requête et renvoie [].

URL liste (filtre type CÔTÉ SERVEUR, transaction=vente, maisons=type 146) :
    /resultats-de-votre-recherche-immobiliere/?transaction=vente&type=146
  → 1 page unique : <div class="listeAnnonce"> (pas de vraie pagination ;
    le param &page= renvoie la même page, donc on ne pagine pas).

Cartes liste (div.listeAnnonce) — pauvres :
  - URL    : .listeAnnoncePhoto a[href]  ou  h3 a[href]   → /bien/{slug}/
  - Titre  : h3 a
  - Loc    : .listeAnnonceDescription  → "VILLE  Maison ..." (ville en tête)
  - Prix   : .listeAnnoncePrix  → "220 500 €"
  Aucun code postal en liste → on visite la page détail.

Page détail (/bien/{slug}/) :
  - Description COMPLÈTE + CP inline : .ficheAnnonceDescription
    → "... au cœur de Plaimpied-Givaudins (18340) ..." ; "Référence 4347"
  - Caractéristiques : div.ficheAnnonceCaracteristiques > ul > li
    → "Surface habitable : 184 m²", "Surface du jardin : 850 m²" (= terrain),
      "Pièces : 5", "Chambres : 4", ...
  - Prix : .ficheAnnoncePrix
  - DPE  : <img src=".../dpe/dpe_{VALEUR}_50-90-150-230-330-450_...jpg">
    → la valeur conso (kWh) située dans les bornes A..G donne la lettre.
  - Photos : img sous /wp-content/uploads/cache/... (hors /dpe/)

Filtre département (0 fuite, STRICT) : le CP du bien n'est pas garanti en clair.
Stratégie en cascade pour obtenir le département :
  1. CP trouvé dans la description détail "(18340)" → dept = CP[:2].
  2. sinon, on résout la ville (Secteur / ville du titre) via geo.api.gouv.fr
     (commune → codeDepartement). API publique déjà utilisée ailleurs dans le projet.
Un bien dont le département résolu n'est pas dans la liste cible est REJETÉ.
Un bien dont le département reste indéterminé est REJETÉ (prudence anti-fuite).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.carmin-immobilier.fr"
SEARCH_URL = BASE_URL + "/resultats-de-votre-recherche-immobiliere/"
GEO_API = "https://geo.api.gouv.fr/communes"
MAX_DETAILS = 60          # garde-fou : nb max de fiches détail visitées
PHOTOS_PER_CARD = 12

# Carmin est une agence du Cher (18). Si 18 n'est pas ciblé, on ne scrape pas.
AGENCY_DEPT = "18"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Bornes A..G du DPE encodées dans le nom de fichier (kWh/m²/an).
_DPE_BOUNDS = [50, 90, 150, 230, 330, 450]
_DPE_LETTERS = ["A", "B", "C", "D", "E", "F", "G"]

_EXCLUDE_TITLE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|studio|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Agence mono-département : inutile (et anti-fuite) de scraper si 18 hors zone.
    if AGENCY_DEPT not in departements:
        print(
            f"[Carmin] Dept {AGENCY_DEPT} hors cible {departements} — agence ignorée"
        )
        return []

    results: list[dict] = []
    geo_cache: dict[str, str | None] = {}

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        cards = await _list_maisons(client)
        print(f"[Carmin] {len(cards)} maisons en liste")

        for card in cards[:MAX_DETAILS]:
            try:
                bien = await _build_bien(client, card, departements, geo_cache)
            except Exception as e:
                print(f"[Carmin] Erreur fiche {card.get('url')}: {e}")
                continue
            if not bien:
                continue

            # Filtre département STRICT (0 fuite)
            if bien["departement"] not in departements:
                continue
            if bien["code_postal"] and bien["code_postal"][:2] not in departements:
                continue

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            results.append(bien)
            await asyncio.sleep(0.5)

    print(f"[Carmin] {len(results)} biens retenus")
    return results


async def _list_maisons(client: httpx.AsyncClient) -> list[dict]:
    """Renvoie la liste brute des cartes maisons (vente, type=146)."""
    params = {"transaction": "vente", "type": "146"}
    r = await client.get(SEARCH_URL, params=params)
    if r.status_code != 200:
        print(f"[Carmin] Liste status {r.status_code}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    cards: list[dict] = []
    seen: set[str] = set()
    for el in soup.select("div.listeAnnonce"):
        link = el.select_one(".listeAnnoncePhoto a[href]") or el.select_one("h3 a[href]")
        href = link.get("href", "") if link else ""
        if not href or href in seen:
            continue
        url = href if href.startswith("http") else BASE_URL + href

        h3 = el.select_one("h3 a")
        titre = h3.get_text(" ", strip=True) if h3 else ""
        if _EXCLUDE_TITLE.search(titre):
            # liste « maisons » mais quelques intitulés « Local ou Maison » → on garde
            # seulement si « maison » présent et pas un pur appartement/terrain
            if "maison" not in titre.lower():
                continue

        desc_el = el.select_one(".listeAnnonceDescription")
        liste_desc = desc_el.get_text(" ", strip=True) if desc_el else ""
        prix_el = el.select_one(".listeAnnoncePrix")
        prix = _parse_price(prix_el.get_text(" ", strip=True) if prix_el else "")

        seen.add(href)
        cards.append(
            {"url": url, "titre": titre, "liste_desc": liste_desc, "prix": prix}
        )
    return cards


async def _build_bien(
    client: httpx.AsyncClient,
    card: dict,
    departements: list[str],
    geo_cache: dict[str, str | None],
) -> dict | None:
    r = await client.get(card["url"])
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    # Description complète + référence + CP inline
    desc_el = soup.select_one(".ficheAnnonceDescription")
    description = desc_el.get_text(" ", strip=True) if desc_el else card["liste_desc"]

    ref = ""
    m_ref = re.search(r"R[ée]f[ée]rence\s+([A-Za-z0-9]+)", description)
    if m_ref:
        ref = m_ref.group(1)

    # CP : 1) dans la description "(18340)", 2) résolution geo via ville
    code_postal = ""
    dept = ""
    m_cp = re.search(r"\((\d{5})\)", description)
    if m_cp:
        code_postal = m_cp.group(1)
        dept = code_postal[:2]

    # Caractéristiques structurées
    carac = _parse_caracteristiques(soup)
    secteur = carac.get("secteur", "")

    # Si pas de CP en clair, résoudre la commune (secteur, sinon ville du titre)
    if not dept:
        ville_guess = secteur or _ville_from_title(card["titre"]) or _ville_from_title(
            card["liste_desc"]
        )
        if ville_guess:
            d, cp = await _geo_commune(client, ville_guess, geo_cache)
            if d:
                dept = d
                code_postal = code_postal or cp
        # Indéterminé → on rejette (anti-fuite)
        if not dept:
            return None

    if dept not in departements:
        return None

    ville = secteur or _ville_from_title(card["titre"]) or _ville_from_title(
        card["liste_desc"]
    )
    ville = re.sub(r"\b\d{5}\b", "", ville or "").strip()  # retirer un CP collé

    surface = carac.get("surface")
    surface_terrain = carac.get("terrain")
    pieces = carac.get("pieces")
    chambres = carac.get("chambres")

    # Prix : détail prioritaire, sinon liste
    prix = card["prix"]
    prix_el = soup.select_one(".ficheAnnoncePrix")
    if prix_el:
        p = _parse_price(prix_el.get_text(" ", strip=True))
        if p:
            prix = p

    dpe = _parse_dpe(soup)

    photos = _parse_photos(soup)

    titre = card["titre"] or (f"Maison {ville}".strip())

    return {
        "source": "carmin_immobilier",
        "url": card["url"],
        "id_annonce": ref or card["url"],
        "titre": titre[:150],
        "type_bien": "maison",
        "description": description[:1200],
        "departement": dept,
        "ville": (ville or "")[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Carmin Immobilier",
    }


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _parse_caracteristiques(soup) -> dict:
    """div.ficheAnnonceCaracteristiques > ul > li  ("Label : valeur")."""
    out: dict = {}
    blk = soup.select_one("div.ficheAnnonceCaracteristiques")
    if not blk:
        return out
    for li in blk.find_all("li"):
        txt = li.get_text(" ", strip=True)
        if ":" not in txt:
            continue
        label, _, val = txt.partition(":")
        label = _strip_accents(label).strip().lower()
        val = val.strip()
        if label.startswith("secteur"):
            out["secteur"] = val
        elif "surface habitable" in label:
            out["surface"] = _num(val)
        elif label == "surface" and out.get("surface") is None:
            out["surface"] = _num(val)
        elif "surface du jardin" in label or "terrain" in label:
            out["terrain"] = _num(val)
        elif label.startswith("pieces") or label.startswith("piece"):
            n = _num(val)
            out["pieces"] = int(n) if n else None
        elif label.startswith("chambre"):
            n = _num(val)
            out["chambres"] = int(n) if n else None
    return out


def _parse_dpe(soup) -> str | None:
    for img in soup.select("img"):
        src = img.get("src") or ""
        m = re.search(r"/dpe/dpe_(\d+)_", src)
        if not m:
            continue
        try:
            val = int(m.group(1))
        except ValueError:
            continue
        for i, bound in enumerate(_DPE_BOUNDS):
            if val < bound:
                return _DPE_LETTERS[i]
        return _DPE_LETTERS[-1]
    return None


def _parse_photos(soup) -> list[str]:
    photos: list[str] = []
    seen: set[str] = set()
    for img in soup.select("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "/wp-content/uploads/" not in src:
            continue
        if "/dpe/" in src or src.startswith("data:"):
            continue
        # privilégier les grandes versions (cache c_700x... ou non-thumbnail)
        if src in seen:
            continue
        seen.add(src)
        photos.append(src)
    return photos[:PHOTOS_PER_CARD]


async def _geo_commune(
    client: httpx.AsyncClient, ville: str, cache: dict[str, str | None]
) -> tuple[str | None, str]:
    """Résout commune → (codeDepartement, codePostal) via geo.api.gouv.fr."""
    key = ville.lower().strip()
    if key in cache:
        dept = cache[key]
        return dept, ""
    try:
        r = await client.get(
            GEO_API,
            params={
                "nom": ville,
                "fields": "codeDepartement,codesPostaux",
                "boost": "population",
                "limit": 1,
            },
            headers={"User-Agent": HEADERS["User-Agent"]},
        )
        data = r.json() if r.status_code == 200 else []
    except Exception:
        data = []
    if data:
        dept = data[0].get("codeDepartement")
        cps = data[0].get("codesPostaux") or []
        cache[key] = dept
        return dept, (cps[0] if cps else "")
    cache[key] = None
    return None, ""


def _ville_from_title(text: str) -> str:
    """Ville = mots en MAJUSCULES ou 1er groupe avant un séparateur."""
    if not text:
        return ""
    # "PLAIMPIED GIVAUDINS MAISON ..." → prendre les mots tout en capitales du début
    tokens = text.split()
    caps: list[str] = []
    for tok in tokens:
        letters = re.sub(r"[^A-Za-zÀ-ÿ]", "", tok)
        if letters and letters.upper() == letters and len(letters) > 1:
            caps.append(tok)
        elif caps:
            break
    if caps:
        return " ".join(caps).title()
    # sinon, segment avant une virgule / deux espaces
    seg = re.split(r"[,–-]| {2,}", text)[0].strip()
    # retirer un mot générique en tête
    return seg[:60]


def _strip_accents(s: str) -> str:
    repl = (
        ("é", "e"), ("è", "e"), ("ê", "e"), ("à", "a"), ("â", "a"),
        ("î", "i"), ("ï", "i"), ("ô", "o"), ("û", "u"), ("ç", "c"),
    )
    s = s.lower()
    for a, b in repl:
        s = s.replace(a, b)
    return s


def _num(text: str) -> float | None:
    """'184 m²' → 184.0 ; '1 013 m²' → 1013.0."""
    m = re.search(r"([\d\s\xa0]+)", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        return float(val) if val else None
    except ValueError:
        return None


def _parse_price(text: str) -> float | None:
    # On ne garde que les chiffres avant "honoraires" pour éviter de coller
    # un pourcentage ("5% TTC") au prix.
    digits = re.sub(r"[^\d]", "", text.split("honoraires")[0])
    try:
        return float(digits) if digits else None
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
    print(f"\nTotal Carmin: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    depts_field = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus (CP)    : {depts}")
    print(f"Départements vus (champ) : {depts_field}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal'] or b['departement']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — DPE {b.get('dpe') or '?'}"
            f" — {b['ville']}"
        )

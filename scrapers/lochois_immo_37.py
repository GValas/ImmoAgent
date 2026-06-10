"""scrapers/lochois_immo_37.py — Lochois Immobilier (agence locale Indre-et-Loire)

Méthode : scrape_simple (httpx) — SSR HTML
Site : https://www.lochoisimmo.com/ (agence du Lochois, indép. depuis ~35 ans,
       agences Loches / Bléré / Cormery / Azay-le-Rideau, cœur de zone 37).

URL pattern :
  - Liste complète : /index.php?critere=recherche  (SSR, ~113 cartes brutes,
    inclut les biens VENDU / SOUS COMPROMIS → écartés).
  - Détail        : /{slug}-{id}-{type}.html  (lien direct dans la carte).

Cartes : a.reference_listing
  - secteur : .reference_secteur  → nom de commune en clair (ex "LOCHES",
              "A REIGNAC SUR INDRE", "10 kms Nord de LOCHES", "AZAY-LE-RIDEAU CENTRE",
              "VENDU PAR NOS SOINS"/"SOUS COMPROMIS." pour les biens non dispo).
  - prix    : .reference_prix     → "315 000 € TTC FAI" (ou "VENDU"/"FAIRE OFFRE").
  - titre   : .reference_texte    → "A vendre <TITRE>".
  - photo   : .reference_square100 style background:url(...).

Filtre département — POINT CLÉ : le site n'expose NI code postal NI numéro de
département (ni dans la liste, ni dans la page détail ; les seules coordonnées
lat/lng du HTML sont celles des AGENCES, pas du bien). On ne dispose que du nom
de commune (secteur). Stratégie : on nettoie le secteur (suffixes CENTRE/BOURG,
préfixes "A "/"DE ", bruit de distance "10 kms Nord de", abréviation ST→SAINT),
puis on le résout via l'API officielle geo.api.gouv.fr (commune → codeDepartement
+ code postal). Post-filtre STRICT : on ne garde que les départements cibles.
Un bien dont le secteur n'est pas géocodable (ou hors zone) est écarté → 0 fuite.

Détail (enrichissement, sur les seuls biens retenus) : blocs .Intitule/.Valeur
(Surface habitable, Nombre de pièces, Nombre de chambres), DPE en texte libre,
galerie photos.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.lochoisimmo.com"
LIST_URL = f"{BASE_URL}/index.php?critere=recherche"
GEO_API = "https://geo.api.gouv.fr/communes"
PHOTOS_PER_CARD = 12
MAX_DETAIL = 60  # garde-fou sur le nombre de pages détail visitées

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Statuts de carte sans bien réellement à vendre → ignorés
_SKIP_SECTEUR = re.compile(r"vendu|compromis|sous\s+offre|sous\s+promesse", re.IGNORECASE)

# Types à conserver (maisons / propriétés) vs exclus (terrain, local, garage…)
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|longere|longère|fermette|ferme|pavillon|"
    r"manoir|chateau|château|moulin|demeure|domaine|corps-de-ferme|gite|gîte|grange",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"terrain|local|commerce|garage|parking|immeuble|bureau|fonds|pre|pré|"
    r"hangar|coup-de-peche|coup-de-pêche",
    re.IGNORECASE,
)

_DIR = r"(?:nord|sud|est|ouest|n\.?o\.?|s\.?o\.?|n\.?e\.?|s\.?e\.?)"


def _clean_secteur(secteur: str) -> str:
    """Normalise un libellé secteur en nom de commune géocodable.

    'AZAY-LE-RIDEAU CENTRE' → 'AZAY-LE-RIDEAU'
    'A REIGNAC SUR INDRE'   → 'REIGNAC SUR INDRE'
    '10 kms Nord de LOCHES' → 'LOCHES'
    'A TAUXIGNY ST BAULD'   → 'TAUXIGNY SAINT BAULD'
    """
    s = " " + secteur.strip() + " "
    s = re.sub(r"\b\d+\s*(?:kms?|km|mn|min|minutes)\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\b" + _DIR + r"\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(centre|bourg|ville|secteur|proche)\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\bST\b", "SAINT", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^(?:A|AU|AUX|DE|DU|EN)\s+", "", s, flags=re.IGNORECASE)
    return s.strip(" .,-")


async def _geocode(client: httpx.AsyncClient, nom: str) -> tuple[str, str] | None:
    """Commune → (code_departement, code_postal) via geo.api.gouv.fr. None si KO."""
    if not nom:
        return None
    try:
        r = await client.get(
            GEO_API,
            params={
                "nom": nom,
                "fields": "codeDepartement,codesPostaux",
                "limit": 1,
                "boost": "population",
            },
            timeout=15,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data:
            return None
        dept = data[0].get("codeDepartement")
        cps = data[0].get("codesPostaux") or []
        cp = cps[0] if cps else ""
        if not dept:
            return None
        return dept, cp
    except Exception:
        return None


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    geo_cache: dict[str, tuple[str, str] | None] = {}

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        try:
            r = await client.get(LIST_URL)
        except Exception as e:
            print(f"[LochoisImmo] Erreur liste: {e}")
            return results
        if r.status_code != 200:
            print(f"[LochoisImmo] Liste status {r.status_code}")
            return results

        cards = BeautifulSoup(r.text, "html.parser").select("a.reference_listing")
        print(f"[LochoisImmo] {len(cards)} cartes brutes")

        detail_count = 0
        for card in cards:
            base = _parse_card(card)
            if not base:
                continue

            # Résolution département via le nom de commune (cache)
            cle = base["_secteur_clean"]
            if cle not in geo_cache:
                geo_cache[cle] = await _geocode(client, cle)
                await asyncio.sleep(0.25)
            geo = geo_cache[cle]
            if not geo:
                continue
            dept, cp = geo

            # Post-filtre STRICT département cible
            if dept not in departements:
                continue

            # Bornes prix
            p = base.get("prix") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue

            bien = {
                "source": "lochois_immo_37",
                "url": base["url"],
                "id_annonce": base["id_annonce"],
                "titre": base["titre"][:150],
                "type_bien": base["type_bien"],
                "description": base["description"][:1200],
                "departement": dept,
                "ville": base["ville"][:80],
                "code_postal": cp,
                "surface": None,
                "surface_terrain": None,
                "pieces": None,
                "chambres": None,
                "prix": base["prix"],
                "photos": base["photos"],
                "dpe": None,
                "agence": "Lochois Immobilier",
            }

            # Enrichissement page détail (surface / pièces / DPE / photos)
            if detail_count < MAX_DETAIL:
                try:
                    await _enrich_detail(client, bien)
                except Exception:
                    pass
                detail_count += 1
                await asyncio.sleep(0.3)

            # Borne surface (après enrichissement ; on ne jette pas si inconnue)
            s = bien.get("surface") or 0
            if surface_min and s and s < surface_min:
                continue

            results.append(bien)

    print(f"[LochoisImmo] {len(results)} annonces retenues (zone cible)")
    return results


def _parse_card(card) -> dict | None:
    href = card.get("href", "") or ""
    if not href:
        return None
    url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

    # Secteur (commune)
    sec_el = card.select_one(".reference_secteur")
    secteur = sec_el.get_text(" ", strip=True) if sec_el else ""
    secteur = re.sub(r"location_on", "", secteur).strip()
    if not secteur or _SKIP_SECTEUR.search(secteur):
        return None
    secteur_clean = _clean_secteur(secteur)
    if not secteur_clean:
        return None

    # Type de bien depuis le slug
    slug = re.sub(r"^/?", "", href)
    if _EXCLUDE_TYPE.search(slug) and not _KEEP_TYPE.search(slug):
        return None
    if not _KEEP_TYPE.search(slug):
        return None
    m_type = _KEEP_TYPE.search(slug)
    type_bien = m_type.group(0).lower() if m_type else "maison"

    # id annonce : '...-5814-1.html' → 5814
    id_annonce = url
    m_id = re.search(r"-(\d+)-\d+\.html$", href)
    if m_id:
        id_annonce = m_id.group(1)

    # Prix
    prix_el = card.select_one(".reference_prix")
    prix = _parse_price(prix_el.get_text(" ", strip=True) if prix_el else "")

    # Titre
    txt_el = card.select_one(".reference_texte")
    titre = txt_el.get_text(" ", strip=True) if txt_el else ""
    titre = re.sub(r"^A\s+vendre\s*", "", titre, flags=re.IGNORECASE).strip()
    if not titre:
        titre = f"{type_bien.title()} {secteur}".strip()

    # Photo de carte (background-url)
    photos: list[str] = []
    bg_el = card.select_one(".reference_square100")
    if bg_el:
        m_bg = re.search(r"url\(([^)]+)\)", bg_el.get("style", ""))
        if m_bg:
            src = m_bg.group(1).strip("'\" ")
            if src and not src.startswith("data:"):
                photos.append(src)

    # Ville lisible = secteur nettoyé en Title Case
    ville = secteur_clean.title()

    return {
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre,
        "type_bien": type_bien,
        "description": titre,
        "ville": ville,
        "prix": prix,
        "photos": photos,
        "_secteur_clean": secteur_clean,
    }


async def _enrich_detail(client: httpx.AsyncClient, bien: dict) -> None:
    r = await client.get(bien["url"])
    if r.status_code != 200:
        return
    soup = BeautifulSoup(r.text, "html.parser")

    # Caractéristiques : blocs .Intitule + .Valeur
    for d in soup.find_all("div"):
        it = d.select_one(".Intitule")
        va = d.select_one(".Valeur")
        if not it or not va:
            continue
        label = it.get_text(" ", strip=True).lower()
        value = va.get_text(" ", strip=True)
        if "surface habitable" in label and bien["surface"] is None:
            bien["surface"] = _parse_float_first(value)
        elif "surface" in label and "terrain" in label and bien["surface_terrain"] is None:
            bien["surface_terrain"] = _parse_float_first(value)
        elif "nombre de pi" in label and bien["pieces"] is None:
            bien["pieces"] = _parse_int_first(value)
        elif "nombre de chambres" in label and bien["chambres"] is None:
            bien["chambres"] = _parse_int_first(value)

    # DPE : "DPE : F indice 391"
    txt = soup.get_text(" ", strip=True)
    m_dpe = re.search(r"DPE\s*:?\s*([A-G])\b", txt)
    if m_dpe:
        bien["dpe"] = m_dpe.group(1).upper()

    # Terrain en texte libre si non trouvé en caractéristique
    if bien["surface_terrain"] is None:
        m_ter = re.search(
            r"terrain[^0-9]{0,30}?([\d\s\xa0]+)\s*m[²2]", txt, re.IGNORECASE
        )
        if m_ter:
            bien["surface_terrain"] = _parse_float_first(m_ter.group(1) + " m2")

    # Description plus riche : paragraphe de présentation
    desc_el = soup.select_one("#contenu_int p, .description, .reference_descriptif")
    if desc_el:
        desc = desc_el.get_text(" ", strip=True)
        if len(desc) > len(bien["description"]):
            bien["description"] = desc[:1200]

    # Galerie photos
    for img in soup.select("img"):
        src = img.get("data-src") or img.get("src") or ""
        if "/catalogue/" in src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            if src not in bien["photos"]:
                bien["photos"].append(src)
    bien["photos"] = bien["photos"][:PHOTOS_PER_CARD]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    if not text or not re.search(r"\d", text):
        return None
    cleaned = re.sub(r"[^\d]", "", text.split("€")[0])
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_float_first(text: str) -> float | None:
    m = re.search(r"([\d\s\xa0]+(?:[.,]\d+)?)", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
    try:
        f = float(val)
        return f if f > 0 else None
    except ValueError:
        return None


def _parse_int_first(text: str) -> int | None:
    m = re.search(r"(\d+)", text)
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
    print(f"\nTotal Lochois Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — DPE {b.get('dpe') or '?'}"
            f" — {b['type_bien']} — {b['ville']}"
            f" — {len(b['photos'])} photos"
        )

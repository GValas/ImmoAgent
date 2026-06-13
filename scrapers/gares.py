"""
scrapers/gares.py — Référentiel des gares SNCF voyageurs (open data, sans clé API)
Source : ressources.data.sncf.com — dataset `liste-des-gares` (filtre voyageurs == "O")
Géocodage commune : geo.api.gouv.fr (gratuit, sans clé)

Permet d'enrichir chaque bien avec la gare voyageurs la plus proche et de filtrer
les biens dont aucune gare ne se trouve dans un rayon donné.

Interface standard : async def search(criteres: dict) -> list[dict]
  → Retourne toujours [] (ce n'est pas une source d'annonces actives)

Interface utilitaire :
  async def get_gares() -> list[dict]
      → [{"nom", "commune", "lat", "lon"}, ...] (mis en cache de session)
  async def annotate_biens(biens: list[dict], rayon_km: float) -> list[dict]
      → ajoute gare / gare_nom / gare_distance_km à chaque bien (in place)
  async def filter_biens_gare(biens, rayon_km) -> list[dict]
      → annote puis ne conserve que les biens avec gare <= rayon_km
"""
import asyncio
import math
import unicodedata

import httpx

SNCF_EXPORT_URL = (
    "https://ressources.data.sncf.com/api/explore/v2.1/catalog/"
    "datasets/liste-des-gares/exports/json"
)
GEO_API_URL = "https://geo.api.gouv.fr/communes"

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# Caches de session
_GARES_CACHE: list[dict] = []
_GEOCODE_CACHE: dict[str, tuple[float, float] | None] = {}


def _normalize(s: str) -> str:
    """Minuscules, sans accents, espaces compactés — pour comparer des noms de commune."""
    s = unicodedata.normalize("NFKD", (s or "").strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.replace("-", " ").split())


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance grand-cercle en km entre deux points (degrés décimaux)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ──────────────────────────────────────────────
# Référentiel gares SNCF
# ──────────────────────────────────────────────

async def get_gares() -> list[dict]:
    """Télécharge (une fois) toutes les gares voyageurs avec coordonnées."""
    if _GARES_CACHE:
        return _GARES_CACHE

    params = {"where": 'voyageurs="O"', "select": "libelle,commune,c_geo"}
    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=60) as client:
        r = await client.get(SNCF_EXPORT_URL, params=params)
        r.raise_for_status()
        rows = r.json()

    for row in rows:
        geo = row.get("c_geo") or {}
        lat, lon = geo.get("lat"), geo.get("lon")
        if lat is None or lon is None:
            continue
        _GARES_CACHE.append({
            "nom": row.get("libelle") or row.get("commune") or "",
            "commune": _normalize(row.get("commune", "")),
            "lat": float(lat),
            "lon": float(lon),
        })

    print(f"[Gares] {len(_GARES_CACHE)} gares voyageurs chargées (SNCF open data)")
    return _GARES_CACHE


# ──────────────────────────────────────────────
# Géocodage des communes (fallback si le bien n'a pas de coordonnées)
# ──────────────────────────────────────────────

async def _geocode_commune(
    client: httpx.AsyncClient, ville: str, code_postal: str, departement: str = ""
) -> tuple[float, float] | None:
    """
    Renvoie (lat, lon) du centre de la commune via geo.api.gouv.fr, mis en cache.
    Le code postal lève l'ambiguïté entre communes homonymes ; à défaut on retombe
    sur le département (présent à 100% dans les biens) pour éviter de géocoder une
    commune homonyme à l'autre bout de la France.
    """
    cp = str(code_postal or "").strip()
    dep = str(departement or "").strip()
    key = f"{_normalize(ville)}|{cp[:5]}|{dep}"
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]

    # Il faut une VILLE ou un CODE POSTAL complet pour localiser une commune.
    # Le département seul ne suffit PAS : l'API renvoie alors une commune
    # arbitraire (la 1ʳᵉ du dept, ex. « Allonnes » pour le 49) → localisation
    # bidon. Dans ce cas on renvoie None (le bien sera exclu, faute de position).
    params = {"fields": "centre", "format": "json", "limit": "1"}
    if ville:
        params["nom"] = ville
        if len(cp) >= 5:
            params["codePostal"] = cp[:5]      # commune précise
        elif dep:
            params["codeDepartement"] = dep    # désambigue les homonymes
    elif len(cp) >= 5:
        params["codePostal"] = cp[:5]          # commune identifiée par le seul CP
    else:
        _GEOCODE_CACHE[key] = None             # département seul → pas localisable
        return None

    coords = None
    try:
        r = await client.get(GEO_API_URL, params=params)
        if r.status_code == 200 and r.json():
            centre = r.json()[0].get("centre", {}).get("coordinates")
            if centre:  # [lon, lat]
                coords = (float(centre[1]), float(centre[0]))
    except Exception as e:
        print(f"[Gares] géocodage échoué pour {ville} ({code_postal}): {e}")

    _GEOCODE_CACHE[key] = coords
    return coords


# ──────────────────────────────────────────────
# Enrichissement + filtre
# ──────────────────────────────────────────────

def _nearest_gare(lat: float, lon: float, gares: list[dict]) -> tuple[dict, float]:
    """Gare la plus proche et sa distance (km)."""
    best, best_d = None, float("inf")
    for g in gares:
        d = _haversine_km(lat, lon, g["lat"], g["lon"])
        if d < best_d:
            best, best_d = g, d
    return best, best_d


async def annotate_biens(biens: list[dict], rayon_km: float = 10.0) -> list[dict]:
    """
    Ajoute à chaque bien (in place) :
      - gare (bool)            : une gare voyageurs est <= rayon_km
      - gare_nom (str)         : nom de la gare la plus proche
      - gare_distance_km (float)
    Utilise les coordonnées du bien si présentes, sinon géocode la commune.
    """
    gares = await get_gares()
    if not gares:
        print("[Gares] référentiel vide — annotation ignorée")
        return biens

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=30) as client:
        # Géocodage parallèle (limité) des communes sans coordonnées
        sem = asyncio.Semaphore(16)   # géocodage commune (geo.api.gouv.fr tolère)

        async def coords_for(b: dict) -> tuple[float, float] | None:
            lat, lon = b.get("latitude"), b.get("longitude")
            if lat is not None and lon is not None:
                return (float(lat), float(lon))
            async with sem:
                return await _geocode_commune(
                    client, b.get("ville", ""), b.get("code_postal", ""),
                    b.get("departement", ""),
                )

        all_coords = await asyncio.gather(*(coords_for(b) for b in biens))

    for b, coords in zip(biens, all_coords):
        if not coords:
            b["gare"] = False
            b["gare_nom"] = None
            b["gare_distance_km"] = None
            continue
        gare, dist = _nearest_gare(coords[0], coords[1], gares)
        b["gare"] = dist <= rayon_km
        b["gare_nom"] = gare["nom"] if gare else None
        b["gare_distance_km"] = round(dist, 1)

    return biens


async def filter_biens_gare(biens: list[dict], rayon_km: float = 10.0) -> list[dict]:
    """Annote puis ne conserve que les biens avec une gare voyageurs <= rayon_km."""
    annotated = await annotate_biens(biens, rayon_km)
    kept = [b for b in annotated if b.get("gare")]
    print(f"[Gares] Filtre gare (<= {rayon_km} km) : {len(kept)} conservés | "
          f"{len(annotated) - len(kept)} exclus")
    return kept


async def search(criteres: dict) -> list[dict]:
    """Pas une source d'annonces — retourne toujours []."""
    return []


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    demo = [
        {"ville": "Le Mans", "code_postal": "72000"},
        {"ville": "Vibraye", "code_postal": "72320"},
        {"ville": "La Ferté-Bernard", "code_postal": "72400"},
        {"ville": "Saint-Calais", "code_postal": "72120"},
        {"ville": "Montmirail", "code_postal": "72320"},
    ]
    print("Test enrichissement gares...\n")
    out = asyncio.run(filter_biens_gare(demo, rayon_km=10))
    print("\n=== Résultat ===")
    for b in asyncio.run(annotate_biens(demo, rayon_km=10)):
        flag = "✓" if b["gare"] else "✗"
        print(f"  {flag} {b['ville']:<20} → {b.get('gare_nom')} "
              f"({b.get('gare_distance_km')} km)")

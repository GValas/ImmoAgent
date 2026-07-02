"""core/geo.py — Utilitaires géographiques partagés.

Une seule implémentation de la normalisation des noms de commune, de la distance
haversine et du géocodage commune (geo.api.gouv.fr) avec UN cache de session —
auparavant bus.py et gares.py portaient chacun une copie ET un cache séparé,
donc chaque commune était géocodée deux fois par run.

La sémantique du géocodage est celle (stricte) de l'ex-gares.py : il faut une
VILLE ou un CODE POSTAL complet ; le département seul ne suffit pas (l'API
renverrait une commune arbitraire du département → localisation bidon).
"""
from __future__ import annotations

import math
import unicodedata

import httpx

GEO_API_URL = "https://geo.api.gouv.fr/communes"

# Cache de session UNIQUE, partagé gares / bus / geolocate.
_GEOCODE_CACHE: dict[str, tuple[float, float] | None] = {}


def normalize_commune(s: str) -> str:
    """Minuscules, sans accents, espaces compactés — pour comparer des noms de commune."""
    s = unicodedata.normalize("NFKD", (s or "").strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.replace("-", " ").split())


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance grand-cercle en km entre deux points (degrés décimaux)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


async def geocode_commune(
    client: httpx.AsyncClient, ville: str, code_postal: str, departement: str = ""
) -> tuple[float, float] | None:
    """
    Renvoie (lat, lon) du centre de la commune via geo.api.gouv.fr, mis en cache.
    Le code postal lève l'ambiguïté entre communes homonymes ; à défaut on retombe
    sur le département (présent à ~100% dans les biens) pour éviter de géocoder une
    commune homonyme à l'autre bout de la France.
    """
    cp = str(code_postal or "").strip()
    dep = str(departement or "").strip()
    key = f"{normalize_commune(ville)}|{cp[:5]}|{dep}"
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]

    # Il faut une VILLE ou un CODE POSTAL complet pour localiser une commune.
    # Le département seul ne suffit PAS : l'API renvoie alors une commune
    # arbitraire (la 1ʳᵉ du dept, ex. « Allonnes » pour le 49) → localisation
    # bidon. Dans ce cas on renvoie None (pas de position exploitable).
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
        print(f"[Geo] géocodage échoué pour {ville} ({code_postal}): {e}")

    _GEOCODE_CACHE[key] = coords
    return coords

"""
scrapers/geolocate.py — Pré-localisation cadastrale (open data IGN, sans clé API)

Aide à retrouver l'emplacement précis d'un bien à partir de :
  1. coordonnées approximatives fournies par l'annonce (Bien'ici expose blurInfo :
     un centre + un rayon de floutage ~125 m) ;
  2. la surface du terrain annoncée, croisée avec le cadastre (parcelles + contenance).

Sources, toutes gratuites et sans authentification :
  - apicarto cadastre IGN : https://apicarto.ign.fr/api/cadastre/parcelle
        → parcelles intersectant une géométrie, avec `contenance` (m²)
  - géocodage commune     : geo.api.gouv.fr (repli si le bien n'a pas de coordonnées)

Important : on n'automatise PAS le scraping des tuiles Google Maps (hors-CGU pour de
la vision auto). Les liens Google/Geoportail produits sont destinés au clic humain.

Interface standard : async def search(criteres: dict) -> list[dict]
  → Retourne toujours [] (ce n'est pas une source d'annonces actives)

Interface utilitaire :
  maps_links(lat, lon) -> dict
      → liens cliquables (Google satellite, Geoportail ortho+cadastre, cadastre.gouv)
  async annotate_biens(biens, criteres) -> list[dict]
      → enrichit chaque bien (in place) avec liens + parcelles candidates
"""
import asyncio
import json
import math
import re
import sys
from pathlib import Path

import httpx

# Racine du projet sur le path (permet `python scrapers/geolocate.py` en direct)
sys.path.insert(0, str(Path(__file__).parent.parent))

APICARTO_PARCELLE = "https://apicarto.ign.fr/api/cadastre/parcelle"

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# Repli sur le géocodage commune du module gares (mêmes caches de session)
from scrapers.gares import _geocode_commune, _haversine_km  # noqa: E402

# Rayon de recherche par défaut si l'annonce ne fournit pas de rayon de floutage (m)
DEFAULT_RADIUS_M = 150.0
# Tolérance par défaut sur l'écart de contenance vs surface terrain annoncée (%)
DEFAULT_TERRAIN_TOL_PCT = 25.0
# Nombre max de parcelles candidates conservées par bien
MAX_CANDIDATES = 5
# Plafond apicarto (la couche cadastre renvoie au plus ~200 features par requête)
APICARTO_LIMIT = 200


# ──────────────────────────────────────────────
# Géométrie
# ──────────────────────────────────────────────

def _circle_polygon(lat: float, lon: float, r_m: float, n: int = 24) -> dict:
    """Polygone GeoJSON (EPSG:4326) approximant un disque de rayon r_m autour du point."""
    pts = []
    for i in range(n + 1):
        a = 2 * math.pi * i / n
        dlat = (r_m * math.cos(a)) / 111320.0
        dlon = (r_m * math.sin(a)) / (111320.0 * math.cos(math.radians(lat)))
        pts.append([lon + dlon, lat + dlat])
    return {"type": "Polygon", "coordinates": [pts]}


def _ring_centroid(coords) -> tuple[float, float]:
    """
    Centroïde (lat, lon) approché d'une géométrie GeoJSON (Point/Polygon/MultiPolygon)
    par moyenne de tous ses sommets [lon, lat], quelle que soit la profondeur.
    """
    pts: list[tuple[float, float]] = []

    def walk(node):
        if (isinstance(node, (list, tuple)) and len(node) == 2
                and all(isinstance(v, (int, float)) for v in node)):
            pts.append((node[0], node[1]))  # [lon, lat]
        elif isinstance(node, (list, tuple)):
            for child in node:
                walk(child)

    walk(coords)
    if not pts:
        return 0.0, 0.0
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    return sy / len(pts), sx / len(pts)  # (lat, lon)


def _geom_span_m(coords, lat: float) -> float:
    """Plus grande dimension (m) de la boîte englobante d'une géométrie GeoJSON."""
    pts: list[tuple[float, float]] = []

    def walk(node):
        if (isinstance(node, (list, tuple)) and len(node) == 2
                and all(isinstance(v, (int, float)) for v in node)):
            pts.append((node[0], node[1]))
        elif isinstance(node, (list, tuple)):
            for child in node:
                walk(child)

    walk(coords)
    if not pts:
        return 0.0
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    h = (max(lats) - min(lats)) * 111320.0
    w = (max(lons) - min(lons)) * 111320.0 * max(math.cos(math.radians(lat)), 1e-6)
    return max(h, w)


# ──────────────────────────────────────────────
# Liens cliquables (pour l'œil humain)
# ──────────────────────────────────────────────

def maps_links(lat: float, lon: float) -> dict:
    """
    Construit les liens de localisation centrés sur (lat, lon).
    - google_satellite : vue satellite Google Maps zoom 19
    - geoportail       : ortho IGN + parcellaire cadastral superposés (idéal pour la technique)
    - cadastre         : carte cadastre.data.gouv.fr en fond ortho
    """
    return {
        "google_satellite": f"https://www.google.com/maps/@{lat:.6f},{lon:.6f},19z/data=!3m1!1e3",
        "geoportail": (
            f"https://www.geoportail.gouv.fr/carte?c={lon:.6f},{lat:.6f}&z=19"
            "&l0=ORTHOIMAGERY.ORTHOPHOTOS::GEOPORTAIL:OGC:WMTS(1)"
            "&l1=CADASTRALPARCELS.PARCELLAIRE_EXPRESS::GEOPORTAIL:OGC:WMTS(1)"
            "&permalink=yes"
        ),
        "cadastre": f"https://cadastre.data.gouv.fr/map?style=ortho#19/{lat:.6f}/{lon:.6f}",
    }


# ──────────────────────────────────────────────
# Cadastre — parcelles candidates par surface terrain
# ──────────────────────────────────────────────

async def candidate_parcels(
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    radius_m: float,
    terrain_m2: float | None,
    tol_pct: float = DEFAULT_TERRAIN_TOL_PCT,
) -> list[dict]:
    """
    Renvoie les parcelles du disque (lat, lon, radius_m) classées par pertinence.
    Si terrain_m2 est connu, ne conserve que celles dont la contenance est dans
    ±tol_pct, classées par écart croissant. Sinon classe par contenance décroissante.

    Chaque parcelle : {section, numero, contenance, lat, lon, ecart_pct, dist_m}.
    """
    geom = json.dumps(_circle_polygon(lat, lon, radius_m))
    try:
        r = await client.get(APICARTO_PARCELLE, params={"geom": geom, "_limit": str(APICARTO_LIMIT)})
        if r.status_code != 200:
            return []
        feats = r.json().get("features", [])
    except Exception as e:
        print(f"[Geoloc] apicarto échec ({lat:.4f},{lon:.4f}): {e}")
        return []

    parcels = []
    for f in feats:
        p = f.get("properties", {}) or {}
        cont = p.get("contenance")
        if not cont:
            continue
        geom = f.get("geometry", {}).get("coordinates", [])
        c_lat, c_lon = _ring_centroid(geom)
        ecart = abs(cont - terrain_m2) / terrain_m2 * 100 if terrain_m2 else None
        parcels.append({
            "section": p.get("section", ""),
            "numero": p.get("numero", ""),
            "contenance": int(cont),
            "lat": round(c_lat, 6),
            "lon": round(c_lon, 6),
            "span_m": round(_geom_span_m(geom, c_lat)),
            "ecart_pct": round(ecart, 1) if ecart is not None else None,
            "dist_m": round(_haversine_km(lat, lon, c_lat, c_lon) * 1000),
        })

    if terrain_m2:
        parcels = [p for p in parcels if p["ecart_pct"] is not None and p["ecart_pct"] <= tol_pct]
        parcels.sort(key=lambda p: (p["ecart_pct"], p["dist_m"]))
    else:
        # Surface terrain inconnue : la meilleure estimation est la parcelle SOUS le
        # point (coords précises), pas la plus grande du voisinage.
        parcels.sort(key=lambda p: p["dist_m"])

    return parcels[:MAX_CANDIDATES]


# ──────────────────────────────────────────────
# Enrichissement des biens
# ──────────────────────────────────────────────

# Extraction de coordonnées depuis la page détail d'une annonce (sources sans coords
# dans la liste : notaires, era, proprietes_privees, greenacres…). Patrons génériques
# du plus fiable au plus permissif ; validés ensuite contre le centre de la commune.
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}
# Paires (lat, lon) explicitement étiquetées — l'ordre est connu et fiable.
_PAIR_PATTERNS = [
    # "latitude":x,...,"longitude":y  /  "lat":x,"lng":y  (espaces/clés intermédiaires tolérés)
    re.compile(r'"lat(?:itude)?"\s*:\s*"?(-?\d{1,3}[.,]\d{2,})"?[^{}\[\]]{0,60}?"l(?:ng|on|ongitude)"\s*:\s*"?(-?\d{1,3}[.,]\d{2,})', re.I),
    # data-latitude="x" … data-longitude="y"  (citya, certaines SSR)
    re.compile(r'data-lat(?:itude)?\s*=\s*["\'](-?\d{1,3}[.,]\d{2,})["\'][^>]{0,160}?data-l(?:ng|on|ongitude)\s*=\s*["\'](-?\d{1,3}[.,]\d{2,})', re.I),
    # Leaflet/Google JS : L.marker([lat,lon]) / LatLng(lat,lon) / setView([lat,lon])
    re.compile(r'(?:marker|latlng|setview|new\s+google\.maps\.latlng)\s*\(\s*\[?\s*(-?\d{1,3}\.\d{3,})\s*,\s*(-?\d{1,3}\.\d{3,})', re.I),
    # URL Google Maps : @lat,lon  /  q=lat,lon  /  center=lat,lon
    re.compile(r'[@](-?\d{1,2}\.\d{4,})\s*,\s*(-?\d{1,3}\.\d{4,})'),
    re.compile(r'[?&](?:q|ll|center|sll)=(-?\d{1,2}\.\d{4,})\s*,\s*(-?\d{1,3}\.\d{4,})'),
]
# Valeurs étiquetées isolées : lat et lon peuvent être des attributs/champs séparés
# (citya: data-latitude / data-longitude ; megagence: lat: x … lng: y). On les croise.
_LAT_SINGLE = re.compile(r'(?:"lat(?:itude)?"|data-lat(?:itude)?|\blat(?:itude)?)\s*[:=]\s*["\']?(-?\d{1,2}\.\d{2,})', re.I)
_LON_SINGLE = re.compile(r'(?:"l(?:ng|on|ongitude)?"|data-l(?:ng|on|ongitude)|\bl(?:ng|ongitude))\s*[:=]\s*["\']?(-?\d{1,3}\.\d{2,})', re.I)
_SINGLE_MAX = 40   # plafond de valeurs croisées

# Repli : toute paire de décimaux adjacents (ordre inconnu → on testera les 2 sens).
_ADJ_PATTERN = re.compile(r'(-?\d{1,3}\.\d{4,})\s*[,;]\s*(-?\d{1,3}\.\d{4,})')
_ADJ_MAX = 800   # plafond de paires examinées (pages avec beaucoup de nombres)
_FR_LAT = (41.0, 51.5)
_FR_LON = (-5.5, 10.0)
_DETAIL_MAX_KM = 10.0   # au-delà du centre commune, le candidat n'est pas le bien (autre ville/défaut)


_NOTAIRES_DETAIL_API = ("https://www.immobilier.notaires.fr/pub-services/"
                        "inotr-www-annonces/v1/annonces/{id}")


def _in_france(lat: float, lon: float) -> bool:
    return _FR_LAT[0] <= lat <= _FR_LAT[1] and _FR_LON[0] <= lon <= _FR_LON[1]


def _nearest_valid(cands: list[tuple[float, float]],
                   commune: tuple[float, float] | None) -> tuple[float, float] | None:
    """Garde le candidat le plus proche du centre commune (<= _DETAIL_MAX_KM)."""
    if not cands:
        return None
    if commune:
        clat, clon = commune
        cands = [c for c in cands if _haversine_km(clat, clon, c[0], c[1]) <= _DETAIL_MAX_KM]
        if not cands:
            return None
        cands.sort(key=lambda c: _haversine_km(clat, clon, c[0], c[1]))
    return cands[0]


async def _notaires_coords(client: httpx.AsyncClient, id_annonce: str,
                           commune: tuple[float, float] | None) -> tuple[float, float] | None:
    """Coordonnées exactes (WGS84) depuis l'API détail Immobilier.notaires.fr."""
    if not id_annonce:
        return None
    try:
        r = await client.get(_NOTAIRES_DETAIL_API.format(id=id_annonce),
                             headers={"Accept": "application/json", "User-Agent": _BROWSER_HEADERS["User-Agent"]},
                             timeout=20, follow_redirects=True)
        if r.status_code != 200:
            return None
        maison = (r.json().get("bien") or {}).get("maison") or {}
        geo = maison.get("coordonneesExactesW84") or {}
        lat, lon = geo.get("coordonneeY"), geo.get("coordonneeX")
        if lat is None or lon is None:
            return None
        lat, lon = float(lat), float(lon)
        if not _in_france(lat, lon):
            return None
        return _nearest_valid([(lat, lon)], commune)
    except Exception:
        return None


async def coords_from_detail(client: httpx.AsyncClient, bien: dict,
                             commune: tuple[float, float] | None) -> tuple[float, float] | None:
    """
    Récupère (lat, lon) propres au bien depuis sa page/API détail, en validant le
    candidat contre le centre de la commune (rejette les centroïdes région/pays).
    Dispatch par source : API dédiée si connue, sinon parsing HTML générique.
    """
    if bien.get("source") == "immobilier_notaires":
        return await _notaires_coords(client, bien.get("id_annonce"), commune)

    url = bien.get("url")
    if not url:
        return None
    try:
        r = await client.get(url, headers=_BROWSER_HEADERS, timeout=20, follow_redirects=True)
        if r.status_code != 200:
            return None
        html = r.text
    except Exception:
        return None
    return _extract_html_coords(html, commune)


def _extract_html_coords(html: str, commune: tuple[float, float] | None) -> tuple[float, float] | None:
    """
    Extrait (lat, lon) d'une page HTML. D'abord les paires étiquetées (ordre fiable) ;
    à défaut, repli sur les paires de décimaux adjacents en testant les deux ordres —
    la validation contre le centre commune lève l'ambiguïté lat/lon et rejette le bruit.
    """
    def _f(s: str) -> float | None:
        try:
            return float(s.replace(",", "."))
        except ValueError:
            return None

    # 1) Paires étiquetées (lat, lon dans cet ordre).
    labeled = []
    for pat in _PAIR_PATTERNS:
        for a, b in pat.findall(html):
            lat, lon = _f(a), _f(b)
            if lat is not None and lon is not None and _in_france(lat, lon):
                labeled.append((lat, lon))
    hit = _nearest_valid(labeled, commune)
    if hit:
        return hit

    # 2) Valeurs lat/lon étiquetées mais séparées → produit croisé (validé commune).
    lats = [v for v in (_f(s) for s in _LAT_SINGLE.findall(html)[:_SINGLE_MAX]) if v is not None]
    lons = [v for v in (_f(s) for s in _LON_SINGLE.findall(html)[:_SINGLE_MAX]) if v is not None]
    if lats and lons:
        crossed = [(la, lo) for la in lats for lo in lons if _in_france(la, lo)]
        hit = _nearest_valid(crossed, commune)
        if hit:
            return hit

    # 3) Repli : paires adjacentes, ordre inconnu → on essaie (a,b) et (b,a).
    #    Nécessite le centre commune pour trancher (sinon trop ambigu).
    if not commune:
        return None
    ambiguous = []
    for a, b in _ADJ_PATTERN.findall(html)[:_ADJ_MAX]:
        x, y = _f(a), _f(b)
        if x is None or y is None:
            continue
        for lat, lon in ((x, y), (y, x)):
            if _in_france(lat, lon):
                ambiguous.append((lat, lon))
    return _nearest_valid(ambiguous, commune)


async def _coords_for(client: httpx.AsyncClient, b: dict) -> tuple[tuple[float, float] | None, float, bool]:
    """
    (coords, radius_m, precis) pour un bien.
    precis=True si les coordonnées sont propres au bien (annonce ou page détail) ;
    sinon repli sur le centre de la commune (radius non pertinent pour le cadastre).
    """
    lat, lon = b.get("latitude"), b.get("longitude")
    if lat is not None and lon is not None:
        radius = float(b.get("blur_radius_m") or 0) or DEFAULT_RADIUS_M
        return (float(lat), float(lon)), radius, True

    commune = await _geocode_commune(client, b.get("ville", ""), b.get("code_postal", ""),
                                     b.get("departement", ""))
    # Tentative d'extraction des coordonnées depuis la page/API détail de l'annonce.
    detail = await coords_from_detail(client, b, commune)
    if detail:
        b["latitude"], b["longitude"] = detail   # mémorise pour l'Excel / réutilisation
        b["geo_source"] = "page_detail"
        return detail, DEFAULT_RADIUS_M, True
    return commune, 0.0, False


async def annotate_biens(biens: list[dict], criteres=None) -> list[dict]:
    """
    Enrichit chaque bien (in place) :
      - maps_satellite_url / geoportail_url / cadastre_url
      - geo_precis (bool)            : coords issues de l'annonce (vs centre commune)
      - parcelles_candidates (list)  : parcelles compatibles avec surface_terrain
      - parcelle_match (str)         : meilleure parcelle "Section Numéro — N m²"
    """
    tol = float(getattr(criteres, "geoloc_terrain_tol_pct", DEFAULT_TERRAIN_TOL_PCT)) if criteres else DEFAULT_TERRAIN_TOL_PCT
    sem = asyncio.Semaphore(6)

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=40) as client:
        async def enrich(b: dict):
            coords, radius, precis = await _coords_for(client, b)
            if not coords:
                b["geo_precis"] = False
                return
            lat, lon = coords
            links = maps_links(lat, lon)
            b["maps_satellite_url"] = links["google_satellite"]
            b["geoportail_url"] = links["geoportail"]
            b["cadastre_url"] = links["cadastre"]
            b["geo_precis"] = precis

            # Parcelles candidates : seulement si la position est précise (sinon le
            # disque couvrirait toute la commune → bruit).
            if not precis:
                return
            terrain = b.get("surface_terrain")
            async with sem:
                parcels = await candidate_parcels(client, lat, lon, radius, terrain, tol)
            b["parcelles_candidates"] = parcels
            if parcels:
                top = parcels[0]
                b["parcelle_match"] = f"{top['section']} {top['numero']} — {top['contenance']} m²"

        await asyncio.gather(*(enrich(b) for b in biens))

    n_loc = sum(1 for b in biens if b.get("parcelle_match"))
    print(f"[Geoloc] {n_loc}/{len(biens)} biens avec parcelle probable")
    return biens


async def search(criteres: dict) -> list[dict]:
    """Pas une source d'annonces — retourne toujours []."""
    return []


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    sys.stdout.reconfigure(encoding="utf-8")

    # Bien témoin : coords issues du blurInfo Bien'ici (Sillé-le-Guillaume), terrain 6000 m²
    demo = [{
        "titre": "Maison test", "ville": "Sillé-le-Guillaume", "code_postal": "72140",
        "departement": "72", "latitude": 48.183788, "longitude": -0.129411,
        "blur_radius_m": 125, "surface_terrain": 6000,
    }]

    class _C:
        geoloc_terrain_tol_pct = 30.0

    out = asyncio.run(annotate_biens(demo, _C()))
    for b in out:
        print("\n=== ", b["titre"], "===")
        print("  satellite :", b.get("maps_satellite_url"))
        print("  geoportail:", b.get("geoportail_url"))
        print("  parcelle  :", b.get("parcelle_match"))
        for p in b.get("parcelles_candidates", []):
            print(f"    - {p['section']} {p['numero']}  {p['contenance']} m²  "
                  f"écart={p['ecart_pct']}%  dist={p['dist_m']}m")

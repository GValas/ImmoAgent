"""
scrapers/geolocate.py — Pré-localisation cadastrale (open data IGN, sans clé API)

Aide à retrouver l'emplacement précis d'un bien à partir de :
  1. coordonnées approximatives fournies par l'annonce (Bien'ici expose blurInfo :
     un centre + un rayon de floutage ~125 m) ;
  2. la surface du terrain annoncée, croisée avec le cadastre (parcelles + contenance) ;
  3. (optionnel, lourd) la détection de piscine sur l'orthophoto IGN.

Sources, toutes gratuites et sans authentification :
  - apicarto cadastre IGN : https://apicarto.ign.fr/api/cadastre/parcelle
        → parcelles intersectant une géométrie, avec `contenance` (m²)
  - orthophoto IGN (WMS)  : https://data.geopf.fr/wms-r/wms (ORTHOIMAGERY.ORTHOPHOTOS)
  - géocodage commune     : geo.api.gouv.fr (repli si le bien n'a pas de coordonnées)

Important : on n'automatise PAS le scraping des tuiles Google Maps (hors-CGU pour de
la vision auto). Les liens Google/Geoportail produits sont destinés au clic humain ;
toute détection automatique se fait sur l'orthophoto IGN (licence ouverte).

Interface standard : async def search(criteres: dict) -> list[dict]
  → Retourne toujours [] (ce n'est pas une source d'annonces actives)

Interface utilitaire :
  maps_links(lat, lon) -> dict
      → liens cliquables (Google satellite, Geoportail ortho+cadastre, cadastre.gouv)
  async annotate_biens(biens, criteres) -> list[dict]
      → enrichit chaque bien (in place) avec liens + parcelles candidates + piscine ortho
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
IGN_WMS = "https://data.geopf.fr/wms-r/wms"
ORTHO_LAYER = "ORTHOIMAGERY.ORTHOPHOTOS"

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
# Orthophoto IGN + détection piscine (heuristique couleur)
# ──────────────────────────────────────────────

async def _fetch_ortho(client: httpx.AsyncClient, lat: float, lon: float,
                       size_m: float = 250.0, px: int = 768) -> bytes | None:
    """Télécharge un carré d'orthophoto IGN (JPEG) de côté size_m centré sur le point."""
    d = (size_m / 2) / 111320.0
    dlon = d / max(math.cos(math.radians(lat)), 1e-6)
    params = {
        "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
        "LAYERS": ORTHO_LAYER, "STYLES": "", "CRS": "EPSG:4326",
        "BBOX": f"{lat-d},{lon-dlon},{lat+d},{lon+dlon}",  # WMS 1.3.0 EPSG:4326 → lat,lon
        "WIDTH": str(px), "HEIGHT": str(px), "FORMAT": "image/jpeg",
    }
    try:
        r = await client.get(IGN_WMS, params=params)
        if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
            return r.content
    except Exception as e:
        print(f"[Geoloc] ortho échec ({lat:.4f},{lon:.4f}): {e}")
    return None


# Seuils d'un blob "piscine", exprimés en m² (donc indépendants de la résolution).
# Un bassin privé fait typiquement ~10–120 m² ; on élargit pour les petits/grands.
_POOL_AREA_M2_MIN = 8.0
_POOL_AREA_M2_MAX = 350.0
_POOL_FILL_MIN = 0.45    # compacité : un bassin remplit bien sa boîte englobante
_POOL_ASPECT_MAX = 6.0   # rejette les traînées très allongées (ombres, bordures)
_CLIP_POOL_THRESHOLD = 0.45   # CLIP seul suffit à confirmer au-delà de ce seuil
_CLIP_SOFT = 0.28             # CLIP modéré + blob franc → piscine (combiné)
_SUBSAMPLE = 2           # sous-échantillonnage du masque pour la rapidité
_ORTHO_PX = 768          # résolution de l'ortho téléchargée
_MAX_BLOBS_CLIP = 5      # nb max de blobs confirmés par CLIP par crop
_MAX_PARCELS_SCAN = 4    # nb max de parcelles candidates scannées par bien


def _pool_color_mask(hsv):
    """Masque booléen des pixels turquoise/bleu piscine (HSV PIL 0–255)."""
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    return (h >= 105) & (h <= 178) & (s >= 60) & (v >= 70)


def _connected_blobs(mask):
    """Composantes connexes (4-voisinage) d'un masque booléen 2D.
    Retourne [{area, bbox=(minr,minc,maxr,maxc)}], sans dépendance scipy."""
    rows, cols = mask.shape
    seen = [[False] * cols for _ in range(rows)]
    m = mask.tolist()
    blobs = []
    for i in range(rows):
        mi, si = m[i], seen[i]
        for j in range(cols):
            if not mi[j] or si[j]:
                continue
            stack = [(i, j)]
            si[j] = True
            area = 0
            minr = maxr = i
            minc = maxc = j
            while stack:
                r, c = stack.pop()
                area += 1
                if r < minr: minr = r
                if r > maxr: maxr = r
                if c < minc: minc = c
                if c > maxc: maxc = c
                for nr, nc in ((r + 1, c), (r - 1, c), (r, c + 1), (r, c - 1)):
                    if 0 <= nr < rows and 0 <= nc < cols and m[nr][nc] and not seen[nr][nc]:
                        seen[nr][nc] = True
                        stack.append((nr, nc))
            blobs.append({"area": area, "bbox": (minr, minc, maxr, maxc)})
    return blobs


def _find_pool_blobs(rgb, size_m: float) -> list[dict]:
    """
    Localise les amas turquoise compacts plausibles (taille en m², compacité,
    allongement) sur une orthophoto carrée RGB couvrant `size_m` mètres de côté.
    Retourne [{area_m2, cx, cy, bbox_full, fill}] en pixels pleine résolution,
    triés par surface décroissante.
    """
    import numpy as np
    hsv = np.asarray(rgb.convert("HSV"))[::_SUBSAMPLE, ::_SUBSAMPLE]
    mask = _pool_color_mask(hsv)
    rows, cols = mask.shape
    if rows == 0 or cols == 0:
        return []
    m_per_px = size_m / cols          # taille d'un pixel sous-échantillonné, en m
    px_area_m2 = m_per_px * m_per_px

    out = []
    for b in _connected_blobs(mask):
        area_m2 = b["area"] * px_area_m2
        if not (_POOL_AREA_M2_MIN <= area_m2 <= _POOL_AREA_M2_MAX):
            continue
        minr, minc, maxr, maxc = b["bbox"]
        bh, bw = maxr - minr + 1, maxc - minc + 1
        fill = b["area"] / (bh * bw)
        aspect = max(bh, bw) / max(1, min(bh, bw))
        if fill < _POOL_FILL_MIN or aspect > _POOL_ASPECT_MAX:
            continue
        # Couleur moyenne des pixels turquoise du blob (discrimine l'eau vive d'un
        # toit ardoise bleu-gris : l'eau est très saturée et claire).
        region_mask = mask[minr:maxr + 1, minc:maxc + 1]
        sel = hsv[minr:maxr + 1, minc:maxc + 1][region_mask]
        mean_h, mean_s, mean_v = (float(sel[:, 0].mean()), float(sel[:, 1].mean()),
                                  float(sel[:, 2].mean())) if len(sel) else (0.0, 0.0, 0.0)
        s = _SUBSAMPLE
        out.append({
            "area_m2": round(area_m2, 1),
            "fill": round(fill, 2),
            "aspect": round(aspect, 2),
            "mean_h": round(mean_h), "mean_s": round(mean_s), "mean_v": round(mean_v),
            "cx": ((minc + maxc) / 2) * s,
            "cy": ((minr + maxr) / 2) * s,
            "bbox_full": (minc * s, minr * s, (maxc + 1) * s, (maxr + 1) * s),
        })
    out.sort(key=lambda b: -b["area_m2"])
    return out


def _blob_is_strong(blob) -> bool:
    """
    Un blob compact à la couleur franche d'eau de piscine — cyan, très saturé et
    clair — est en soi un fort indice (écarte les toits ardoise bleu-gris, ternes).
    """
    return (12.0 <= blob["area_m2"] <= 160.0
            and blob["fill"] >= 0.45 and blob["aspect"] <= 3.5
            and 108 <= blob["mean_h"] <= 150
            and blob["mean_s"] >= 115 and blob["mean_v"] >= 140)


def _pool_decision(rgb, blob) -> float:
    """
    Score de confiance final (0–1) qu'un blob soit une piscine, combinant la
    classification CLIP et la couleur du blob :
      - eau franche (cyan vif et clair) → renforce (CLIP seul rate des piscines) ;
      - bleu terne/sombre type toit ardoise → atténue (sauf CLIP très sûr) ;
      - sinon → score CLIP brut.
    """
    conf = _confirm_pool(rgb, blob)
    if conf < 0:                                   # CLIP indisponible → couleur/forme seule
        return 0.6 if _blob_is_strong(blob) else 0.0
    if _blob_is_strong(blob):                      # cyan vif et compact → forte présomption
        return max(conf, 0.6)
    # NB : la couleur seule ne sépare pas piscine et toit ardoise (une piscine profonde
    # ou ombragée est sombre et peu saturée). On s'en remet à CLIP ; le score gradué
    # affiché dans l'Excel + le lien satellite permettent la vérification à l'œil.
    return conf


def _confirm_pool(rgb, blob) -> float:
    """Confiance CLIP (0–1) que le blob soit une piscine, sur un recadrage centré
    autour du blob (avec marge pour le contexte). Retourne -1 si CLIP indisponible."""
    x0, y0, x1, y1 = blob["bbox_full"]
    pad = max(20, (x1 - x0) // 2)   # marge ∝ taille du blob → contexte pour CLIP
    crop = rgb.crop((max(0, x0 - pad), max(0, y0 - pad),
                     min(rgb.width, x1 + pad), min(rgb.height, y1 + pad)))
    try:
        from agents.vision import clip_pool_confidence
        return clip_pool_confidence(crop)
    except Exception:
        return -1.0


def detect_pool(img_bytes: bytes, size_m: float = 80.0) -> tuple[bool, float]:
    """
    Détecte une piscine sur une orthophoto carrée (detect-then-classify) : localise
    l'amas turquoise compact le plus probant puis décide via CLIP + forme du blob.
    Conservé pour un usage sur crop unique ; `annotate_biens` scanne les parcelles.
    """
    try:
        import io
        from PIL import Image
        rgb = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return False, 0.0
    best = 0.0
    for blob in _find_pool_blobs(rgb, size_m)[:_MAX_BLOBS_CLIP]:
        best = max(best, _pool_decision(rgb, blob))
    return best >= _CLIP_POOL_THRESHOLD, round(best, 3)


async def scan_parcel_for_pool(client: httpx.AsyncClient, parcel: dict) -> dict | None:
    """
    Scanne une orthophoto haute résolution dimensionnée à la parcelle (couvre tout
    le terrain, pas seulement le centroïde) et y cherche une piscine.
    Retourne {lat, lon, score, area_m2} de la meilleure piscine, ou None.
    """
    lat, lon = parcel["lat"], parcel["lon"]
    # Crop ajusté à l'emprise de la parcelle (+ marge), borné pour rester lisible.
    size_m = max(50.0, min((parcel.get("span_m") or 60.0) * 1.4 + 25.0, 200.0))
    img = await _fetch_ortho(client, lat, lon, size_m, _ORTHO_PX)
    if not img:
        return None
    try:
        import io
        from PIL import Image
        rgb = Image.open(io.BytesIO(img)).convert("RGB")
    except Exception:
        return None

    m_per_px = size_m / rgb.width
    cos_lat = max(math.cos(math.radians(lat)), 1e-6)
    best = None
    for blob in _find_pool_blobs(rgb, size_m)[:_MAX_BLOBS_CLIP]:
        score = _pool_decision(rgb, blob)
        if score < _CLIP_POOL_THRESHOLD:
            continue
        dx_m = (blob["cx"] - rgb.width / 2) * m_per_px
        dy_m = (blob["cy"] - rgb.height / 2) * m_per_px
        cand = {
            "lat": round(lat - dy_m / 111320.0, 6),
            "lon": round(lon + dx_m / (111320.0 * cos_lat), 6),
            "score": round(score, 3),
            "area_m2": blob["area_m2"],
        }
        if best is None or cand["score"] > best["score"]:
            best = cand
    return best


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
      - piscine_ortho (bool|None)    : piscine détectée sur l'ortho (si activé)
      - piscine_ortho_score (float)
    """
    tol = float(getattr(criteres, "geoloc_terrain_tol_pct", DEFAULT_TERRAIN_TOL_PCT)) if criteres else DEFAULT_TERRAIN_TOL_PCT
    detect = bool(getattr(criteres, "geoloc_piscine_ortho", False)) if criteres else False
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

            # Détection piscine : scan haute résolution de chaque parcelle candidate
            # (couvre tout le terrain). La parcelle où une piscine est trouvée devient
            # la localisation la plus probable — c'est ta technique, automatisée.
            if detect and parcels:
                best_pool, best_parcel = None, None
                for p in parcels[:_MAX_PARCELS_SCAN]:
                    async with sem:
                        pool = await scan_parcel_for_pool(client, p)
                    p["piscine"] = bool(pool)
                    if pool:
                        p["piscine_score"] = pool["score"]
                        if best_pool is None or pool["score"] > best_pool["score"]:
                            best_pool, best_parcel = pool, p
                b["piscine_ortho"] = best_pool is not None
                b["piscine_ortho_score"] = best_pool["score"] if best_pool else 0.0
                if best_pool:
                    b["piscine_ortho_url"] = maps_links(best_pool["lat"], best_pool["lon"])["google_satellite"]
                    # La parcelle contenant la piscine devient le meilleur candidat.
                    parcels.sort(key=lambda p: (not p.get("piscine"), p.get("ecart_pct") or 1e9))
                    b["parcelles_candidates"] = parcels
                    b["parcelle_match"] = (f"{best_parcel['section']} {best_parcel['numero']} "
                                           f"— {best_parcel['contenance']} m²")

        await asyncio.gather(*(enrich(b) for b in biens))

    n_loc = sum(1 for b in biens if b.get("parcelle_match"))
    print(f"[Geoloc] {n_loc}/{len(biens)} biens avec parcelle probable"
          + (" (détection piscine ortho activée)" if detect else ""))
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
        geoloc_piscine_ortho = True

    out = asyncio.run(annotate_biens(demo, _C()))
    for b in out:
        print("\n=== ", b["titre"], "===")
        print("  satellite :", b.get("maps_satellite_url"))
        print("  geoportail:", b.get("geoportail_url"))
        print("  parcelle  :", b.get("parcelle_match"))
        print("  piscine   :", b.get("piscine_ortho"), b.get("piscine_ortho_score"))
        for p in b.get("parcelles_candidates", []):
            print(f"    - {p['section']} {p['numero']}  {p['contenance']} m²  "
                  f"écart={p['ecart_pct']}%  dist={p['dist_m']}m  "
                  f"piscine={p.get('piscine')} ({p.get('piscine_score')})")

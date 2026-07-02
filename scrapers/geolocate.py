"""
scrapers/geolocate.py — Pré-localisation (open data IGN, sans clé API)

Aide à retrouver l'emplacement précis d'un bien à partir des coordonnées
approximatives fournies par l'annonce (Bien'ici expose blurInfo : un centre + un
rayon de floutage ~125 m), avec repli sur le géocodage de la commune.

Source, gratuite et sans authentification :
  - géocodage commune : geo.api.gouv.fr (repli si le bien n'a pas de coordonnées)

Important : on n'automatise PAS le scraping des tuiles Google Maps (hors-CGU pour de
l'analyse auto). Les liens Google/Geoportail produits sont destinés au clic humain.

Interface standard : async def search(criteres: dict) -> list[dict]
  → Retourne toujours [] (ce n'est pas une source d'annonces actives)

Interface utilitaire :
  maps_links(lat, lon) -> dict
      → liens cliquables (Google satellite, Geoportail ortho+cadastre, cadastre.gouv)
  async annotate_biens(biens, criteres) -> list[dict]
      → enrichit chaque bien (in place) avec ces liens + un flag geo_precis
"""
import asyncio
import re
import sys
from pathlib import Path

import httpx

# Racine du projet sur le path (permet `python scrapers/geolocate.py` en direct)
sys.path.insert(0, str(Path(__file__).parent.parent))

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# Repli sur le géocodage commune du module gares (mêmes caches de session)
from scrapers.gares import _geocode_commune, _haversine_km  # noqa: E402

# Rayon de recherche par défaut si l'annonce ne fournit pas de rayon de floutage (m)
DEFAULT_RADIUS_M = 150.0


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


def rome2rio_url(ville: str, code_postal: str | None = None) -> str:
    """Lien Rome2Rio (planificateur multimodal) Paris → commune : permet de vérifier
    d'un clic le temps de trajet en train/transports depuis Paris (critère
    « accessible par le train »). Lien cliquable seulement — aucun appel API.
    Ne dépend pas des coordonnées : commune + code postal suffisent."""
    from urllib.parse import quote
    ville = (ville or "").strip()
    if not ville:
        return ""
    cp = str(code_postal or "").strip()
    dest = f"{ville} {cp}".strip() if cp else f"{ville}, France"
    return f"https://www.rome2rio.com/s/Paris/{quote(dest)}"


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


def extract_coord_candidates(html: str) -> dict:
    """
    Candidats (lat, lon) d'une page détail : {"labeled": [...], "ambiguous": [...]}.

    `labeled`   : paires explicitement étiquetées (ordre lat/lon fiable), puis
                  produit croisé des valeurs lat/lon étiquetées séparément ;
    `ambiguous` : paires de décimaux adjacents dans LES DEUX ordres — à ne retenir
                  qu'après validation contre le centre commune (≤ _DETAIL_MAX_KM).

    Exposé pour gallery.py : la passe galerie a déjà le HTML détail en main et
    stocke ces candidats sur le bien (`_geo_candidates`) — geolocate n'a alors
    plus besoin de re-télécharger la même page.
    """
    def _f(s: str) -> float | None:
        try:
            return float(s.replace(",", "."))
        except ValueError:
            return None

    labeled: list[tuple[float, float]] = []
    for pat in _PAIR_PATTERNS:
        for a, b in pat.findall(html):
            lat, lon = _f(a), _f(b)
            if lat is not None and lon is not None and _in_france(lat, lon):
                labeled.append((lat, lon))

    lats = [v for v in (_f(s) for s in _LAT_SINGLE.findall(html)[:_SINGLE_MAX]) if v is not None]
    lons = [v for v in (_f(s) for s in _LON_SINGLE.findall(html)[:_SINGLE_MAX]) if v is not None]
    if lats and lons:
        labeled.extend((la, lo) for la in lats for lo in lons if _in_france(la, lo))

    ambiguous: list[tuple[float, float]] = []
    for a, b in _ADJ_PATTERN.findall(html)[:_ADJ_MAX]:
        x, y = _f(a), _f(b)
        if x is None or y is None:
            continue
        for lat, lon in ((x, y), (y, x)):
            if _in_france(lat, lon):
                ambiguous.append((lat, lon))

    return {"labeled": labeled, "ambiguous": ambiguous}


def _coords_from_candidates(cands: dict, commune: tuple[float, float] | None) -> tuple[float, float] | None:
    """Meilleur candidat validé : paires étiquetées d'abord ; les paires ambiguës
    exigent le centre commune pour trancher l'ordre lat/lon (sinon trop risqué)."""
    hit = _nearest_valid([tuple(c) for c in (cands.get("labeled") or [])], commune)
    if hit:
        return hit
    if not commune:
        return None
    return _nearest_valid([tuple(c) for c in (cands.get("ambiguous") or [])], commune)


def _extract_html_coords(html: str, commune: tuple[float, float] | None) -> tuple[float, float] | None:
    """Extrait (lat, lon) d'une page HTML (candidats + validation commune)."""
    return _coords_from_candidates(extract_coord_candidates(html), commune)


async def _coords_for(client: httpx.AsyncClient, b: dict) -> tuple[tuple[float, float] | None, float, bool]:
    """
    (coords, radius_m, precis) pour un bien.
    precis=True si les coordonnées sont propres au bien (annonce ou page détail) ;
    sinon repli sur le centre de la commune (radius non pertinent pour le cadastre).
    """
    lat, lon = b.get("latitude"), b.get("longitude")
    if lat is not None and lon is not None:
        try:
            radius = float(b.get("blur_radius_m") or 0) or DEFAULT_RADIUS_M
            return (float(lat), float(lon)), radius, True
        except (TypeError, ValueError):
            pass   # coords scraper illisibles → repli commune / page détail

    commune = await _geocode_commune(client, b.get("ville", ""), b.get("code_postal", ""),
                                     b.get("departement", ""))

    # 1) Candidats déjà extraits par la passe galerie (gallery.py a téléchargé la
    #    page détail il y a quelques secondes) → validation locale, AUCUN re-fetch.
    cands = b.pop("_geo_candidates", None)
    if cands:
        hit = _coords_from_candidates(cands, commune)
        if hit:
            b["latitude"], b["longitude"] = hit
            b["geo_source"] = "page_detail"
            return hit, DEFAULT_RADIUS_M, True
        # La page a déjà été vue et n'a rien donné de valide → inutile de la
        # re-télécharger (mêmes données) ; repli direct sur le centre commune.
        return commune, 0.0, False

    # 2) Sinon (fetcher galerie dédié sans HTML, coupe-circuit…) : page/API détail.
    detail = await coords_from_detail(client, b, commune)
    if detail:
        b["latitude"], b["longitude"] = detail   # mémorise pour l'Excel / réutilisation
        b["geo_source"] = "page_detail"
        return detail, DEFAULT_RADIUS_M, True
    return commune, 0.0, False


async def annotate_biens(biens: list[dict], criteres=None) -> list[dict]:
    """
    Enrichit chaque bien (in place) :
      - maps_satellite_url / geoportail_url / cadastre_url : liens de vérification
      - geo_precis (bool) : coords issues de l'annonce (vs centre commune)
    """
    from collections import defaultdict
    from urllib.parse import urlparse

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=40) as client:
        # Concurrence bornée : globale + par domaine. Sans cela, N biens d'un même
        # site (ex. foncia) déclenchaient N fetchs détail SIMULTANÉS juste après que
        # gallery s'est limité à 3/domaine — le profil parfait pour un ban 429.
        sem = asyncio.Semaphore(16)
        dom_sems: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(3))

        async def enrich(b: dict):
            # Best-effort : un bien illisible ne doit jamais tuer tout le gather.
            try:
                # Lien accessibilité Rome2Rio (Paris → commune) : indépendant des
                # coords, posé sur tous les biens.
                b["rome2rio_url"] = rome2rio_url(b.get("ville", ""), b.get("code_postal"))
                dom = urlparse(str(b.get("url") or "")).netloc
                async with sem, dom_sems[dom]:
                    coords, _radius, precis = await _coords_for(client, b)
                if not coords:
                    b["geo_precis"] = False
                    return
                lat, lon = coords
                links = maps_links(lat, lon)
                b["maps_satellite_url"] = links["google_satellite"]
                b["geoportail_url"] = links["geoportail"]
                b["cadastre_url"] = links["cadastre"]
                b["geo_precis"] = precis
            except Exception as e:
                b.setdefault("geo_precis", False)
                print(f"[Geoloc] bien ignoré ({type(e).__name__}: {e}) — "
                      f"{str(b.get('url') or b.get('titre') or '')[:60]}")

        await asyncio.gather(*(enrich(b) for b in biens))

    n_loc = sum(1 for b in biens if b.get("geo_precis"))
    print(f"[Geoloc] {n_loc}/{len(biens)} biens géolocalisés précisément")
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

    out = asyncio.run(annotate_biens(demo, None))
    for b in out:
        print("\n=== ", b["titre"], "===")
        print("  satellite :", b.get("maps_satellite_url"))
        print("  geoportail:", b.get("geoportail_url"))
        print("  geo_precis:", b.get("geo_precis"))

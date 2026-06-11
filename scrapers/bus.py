"""
scrapers/bus.py — Arrêts de bus à proximité d'un bien (informatif, NON éliminatoire)

Indique si un arrêt de bus se trouve à proximité d'un bien — vu comme un moyen
complémentaire d'accéder à la gare. Contrairement au filtre gare, c'est purement
informatif et n'élimine aucun bien.

──────────────────────────────────────────────────────────────────────────────
Choix de la source : OpenStreetMap Overpass API (Option B)
──────────────────────────────────────────────────────────────────────────────
Les arrêts de bus sont TRÈS nombreux en France (>500k). Charger le jeu national
en mémoire (option transport.data.gouv.fr) serait lourd et inutile : le pipeline
annote le bus APRÈS le filtre gare, sur les seuls biens survivants
(peu nombreux). On interroge donc Overpass PAR BIEN, sur un petit rayon, ce qui
ne représente que quelques requêtes httpx légères.

On interroge `highway=bus_stop`, `public_transport=platform` et
`public_transport=station` (mode bus) dans un rayon `around:` autour des
coordonnées du bien. Gratuit, sans clé API, licence ODbL (OpenStreetMap).

Non-fatal : si Overpass est indisponible (timeout / 429 / erreur réseau), on
log un warning et on laisse les champs bus vides — le pipeline ne plante jamais.

Interface standard : async def search(criteres: dict) -> list[dict]
  → Retourne toujours [] (ce n'est pas une source d'annonces actives)

Interface utilitaire :
  async def annotate_biens(biens: list[dict], rayon_km: float) -> list[dict]
      → ajoute bus_proche / bus_nom / bus_distance_km à chaque bien (in place)
"""
import asyncio
import math
import os
import time
import unicodedata
import httpx

# Miroir Overpass. (2026-06-10 : kumi.systems était mort et openstreetmap.fr est
#  whitelist-only — 403. On garde le miroir canonique overpass-api.de, qui répond,
#  À CONDITION d'un User-Agent honnête : le spoof « Mozilla/… » renvoyait 406.)
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
]

# Sentinelle : Overpass injoignable (≠ "joignable mais aucun arrêt trouvé").
_OVERPASS_UNAVAILABLE = object()
# Coupe-circuit : si Overpass échoue N fois d'affilée, on cesse de l'appeler pour
# le reste du run (annotation bus non critique) → évite ~80 s perdues par bien.
_BREAKER_THRESHOLD = 3
# Budget temps GLOBAL pour toute l'étape bus (mur, secondes). Overpass public est
# interrogé en quasi-séquentiel (Semaphore(1)) ; sur beaucoup de biens, des
# réponses 200 *lentes* (jamais en échec → coupe-circuit inactif) peuvent étaler
# l'étape indéfiniment. Passé ce budget, on cesse d'appeler Overpass pour les biens
# restants (annotés « pas de bus ») et le pipeline continue. Surcharge : BUS_BUDGET_S.
_DEFAULT_BUDGET_S = float(os.environ.get("BUS_BUDGET_S", "180"))
GEO_API_URL = "https://geo.api.gouv.fr/communes"

# UA honnête : overpass-api.de renvoie 406 sur un User-Agent qui usurpe un navigateur
# (« Mozilla/… ») ; il accepte un UA descriptif. NE PAS remettre un UA Mozilla ici.
_HEADERS = {"User-Agent": "immo-agent/1.0 (personal real-estate search)"}

# Caches de session
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
# Géocodage des communes (fallback si le bien n'a pas de coordonnées)
# ──────────────────────────────────────────────

async def _geocode_commune(
    client: httpx.AsyncClient, ville: str, code_postal: str, departement: str = ""
) -> tuple[float, float] | None:
    """Renvoie (lat, lon) du centre de la commune via geo.api.gouv.fr, mis en cache."""
    cp = str(code_postal or "").strip()
    dep = str(departement or "").strip()
    key = f"{_normalize(ville)}|{cp[:5]}|{dep}"
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]

    params = {"fields": "centre", "format": "json", "limit": "1"}
    if ville:
        params["nom"] = ville
    if len(cp) >= 5:
        params["codePostal"] = cp[:5]
    elif dep:
        params["codeDepartement"] = dep
    elif len(cp) == 2:
        params["codeDepartement"] = cp

    coords = None
    try:
        r = await client.get(GEO_API_URL, params=params)
        if r.status_code == 200 and r.json():
            centre = r.json()[0].get("centre", {}).get("coordinates")
            if centre:  # [lon, lat]
                coords = (float(centre[1]), float(centre[0]))
    except Exception as e:
        print(f"[Bus] géocodage échoué pour {ville} ({code_postal}): {e}")

    _GEOCODE_CACHE[key] = coords
    return coords


# ──────────────────────────────────────────────
# Interrogation Overpass — arrêt de bus le plus proche
# ──────────────────────────────────────────────

def _build_query(lat: float, lon: float, rayon_m: int) -> str:
    """Construit la requête Overpass QL : arrêts de bus dans un rayon autour du point."""
    return (
        f"[out:json][timeout:25];"
        f"("
        f'node(around:{rayon_m},{lat},{lon})["highway"="bus_stop"];'
        f'node(around:{rayon_m},{lat},{lon})["public_transport"="platform"]["bus"="yes"];'
        f'node(around:{rayon_m},{lat},{lon})["public_transport"="station"]["bus"="yes"];'
        f");"
        f"out body;"
    )


def _stop_name(tags: dict) -> str:
    """Nom lisible d'un arrêt : name, sinon ref/route, sinon 'Arrêt de bus'."""
    for k in ("name", "ref:name", "official_name", "alt_name"):
        if tags.get(k):
            return str(tags[k])
    if tags.get("ref"):
        return f"Arrêt {tags['ref']}"
    if tags.get("route_ref"):
        return f"Ligne {tags['route_ref']}"
    return "Arrêt de bus"


async def _nearest_bus(
    client: httpx.AsyncClient, lat: float, lon: float, rayon_m: int
) -> tuple[str, float] | None:
    """
    Renvoie (nom, distance_km) de l'arrêt de bus le plus proche dans le rayon,
    ou None si aucun trouvé / source indisponible (non-fatal).
    """
    query = _build_query(lat, lon, rayon_m)
    elements = None
    # Deux passes (avec petit backoff) × miroirs — les instances publiques
    # renvoient 429 sous charge ; un court délai suffit souvent à passer.
    for attempt in range(2):
        for endpoint in OVERPASS_ENDPOINTS:
            try:
                r = await client.post(endpoint, data={"data": query})
                if r.status_code == 200:
                    elements = r.json().get("elements", [])
                    break
                if r.status_code in (429, 504):
                    await asyncio.sleep(1.0)
                # autre code → on tente le miroir suivant
            except Exception:
                continue
        if elements is not None:
            break
        await asyncio.sleep(1.5)

    if elements is None:
        return _OVERPASS_UNAVAILABLE     # injoignable (géré par le coupe-circuit)
    if not elements:
        return None                       # joignable, aucun arrêt dans le rayon

    best_name, best_d = None, float("inf")
    for el in elements:
        elat, elon = el.get("lat"), el.get("lon")
        if elat is None or elon is None:
            continue
        d = _haversine_km(lat, lon, float(elat), float(elon))
        if d < best_d:
            best_d = d
            best_name = _stop_name(el.get("tags", {}))

    if best_name is None:
        return None
    return best_name, round(best_d, 1)


# ──────────────────────────────────────────────
# Enrichissement
# ──────────────────────────────────────────────

async def annotate_biens(
    biens: list[dict], rayon_km: float = 2.0, budget_s: float | None = None
) -> list[dict]:
    """
    Ajoute à chaque bien (in place) :
      - bus_proche (bool)        : un arrêt de bus est <= rayon_km
      - bus_nom (str)            : nom de l'arrêt/ligne le plus proche
      - bus_distance_km (float)
    Utilise les coordonnées du bien si présentes (geoloc), sinon géocode la commune.
    Informatif uniquement : n'élimine aucun bien. Non-fatal en cas d'échec source.

    Borné par un budget temps GLOBAL (mur) : passé `budget_s` secondes (défaut
    _DEFAULT_BUDGET_S / env BUS_BUDGET_S), les biens non encore traités sont annotés
    « pas de bus » sans appel Overpass — garantit que l'étape ne bloque pas le pipeline.
    """
    if not biens:
        return biens

    rayon_m = max(100, int(rayon_km * 1000))
    budget = _DEFAULT_BUDGET_S if budget_s is None else budget_s
    deadline = time.monotonic() + budget

    # Timeout borné : connect 8 s (un miroir mort échoue vite au lieu de hanger),
    # read 28 s (une requête Overpass chargée peut prendre jusqu'à ~25 s côté serveur).
    _to = httpx.Timeout(28.0, connect=8.0)
    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=_to) as client:
        geo_sem = asyncio.Semaphore(8)       # géocodage geo.api.gouv.fr (tolérant)
        # Overpass public demande un accès quasi-séquentiel (sinon 429). Sur les
        # quelques biens survivants annotés ici, le coût total reste faible.
        overpass_sem = asyncio.Semaphore(1)
        breaker = {"fails": 0, "dead": False}   # coupe-circuit Overpass
        budget_state = {"expired": False}       # budget temps global dépassé

        async def coords_for(b: dict) -> tuple[float, float] | None:
            lat, lon = b.get("latitude"), b.get("longitude")
            if lat is not None and lon is not None:
                return (float(lat), float(lon))
            async with geo_sem:
                return await _geocode_commune(
                    client, b.get("ville", ""), b.get("code_postal", ""),
                    b.get("departement", ""),
                )

        def _no_bus(b: dict):
            b["bus_proche"] = False
            b["bus_nom"] = None
            b["bus_distance_km"] = None

        async def annotate_one(b: dict):
            if breaker["dead"] or budget_state["expired"] or time.monotonic() >= deadline:
                _no_bus(b)
                return
            coords = await coords_for(b)
            if not coords or breaker["dead"] or budget_state["expired"]:
                _no_bus(b)
                return
            async with overpass_sem:
                # Budget global / coupe-circuit ont pu tomber pendant l'attente du
                # sémaphore (accès séquentiel) → on ne lance pas une requête de plus.
                if breaker["dead"] or budget_state["expired"]:
                    _no_bus(b)
                    return
                if time.monotonic() >= deadline:
                    if not budget_state["expired"]:
                        budget_state["expired"] = True
                        print(f"[Bus] Budget temps ({budget:.0f}s) dépassé — annotation "
                              f"bus arrêtée pour les biens restants (non critique)")
                    _no_bus(b)
                    return
                res = await _nearest_bus(client, coords[0], coords[1], rayon_m)
            if res is _OVERPASS_UNAVAILABLE:
                breaker["fails"] += 1
                if breaker["fails"] >= _BREAKER_THRESHOLD:
                    breaker["dead"] = True
                    print(f"[Bus] Overpass injoignable {_BREAKER_THRESHOLD}× d'affilée "
                          f"— annotation bus désactivée pour ce run (non critique)")
                _no_bus(b)
            elif res is None:
                breaker["fails"] = 0         # joignable → reset
                _no_bus(b)
            else:
                breaker["fails"] = 0
                nom, dist = res
                b["bus_proche"] = dist <= rayon_km
                b["bus_nom"] = nom
                b["bus_distance_km"] = dist

        await asyncio.gather(*(annotate_one(b) for b in biens))

    nb = sum(1 for b in biens if b.get("bus_proche"))
    print(f"[Bus] {nb}/{len(biens)} bien(s) avec arrêt de bus <= {rayon_km} km")
    return biens


async def search(criteres: dict) -> list[dict]:
    """Pas une source d'annonces — retourne toujours []."""
    return []


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    demo = [
        {"ville": "Le Mans", "code_postal": "72000", "departement": "72"},
        {"ville": "Chartres", "code_postal": "28000", "departement": "28"},
        {"ville": "Orléans", "code_postal": "45000", "departement": "45"},
        {"ville": "Auxerre", "code_postal": "89000", "departement": "89"},
        {"ville": "Angers", "code_postal": "49000", "departement": "49"},
        {"ville": "Tours", "code_postal": "37000", "departement": "37"},
        {"ville": "Châteauroux", "code_postal": "36000", "departement": "36"},
        {"ville": "Bourges", "code_postal": "18000", "departement": "18"},
        {"ville": "Nevers", "code_postal": "58000", "departement": "58"},
        {"ville": "Blois", "code_postal": "41000", "departement": "41"},
        {"ville": "Laval", "code_postal": "53000", "departement": "53"},
    ]
    print("Test enrichissement bus (Overpass)...\n")
    out = asyncio.run(annotate_biens(demo, rayon_km=2))
    print("\n=== Résultat ===")
    for b in out:
        flag = "✓" if b.get("bus_proche") else "✗"
        print(f"  {flag} {b['ville']:<16} → {b.get('bus_nom')} "
              f"({b.get('bus_distance_km')} km)")

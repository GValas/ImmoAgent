"""
scrapers/pap.py — PAP (Particulier à Particulier, pap.fr)

Méthode : api_inoff (httpx) — via un acteur **Apify** (PAP est infranchissable en
direct à cause de Cloudflare ; cf. blacklist/CLAUDE.md). Seule voie viable : un acteur
Apify maintenu qui rend du JSON.

Acteur retenu : `azzouzana/pap-fr-mass-products-scraper-by-search-url`
  - public, maintenu activement (mis à jour quotidiennement), >12k runs
  - input : { "startUrl": <URL de recherche PAP>, "maxItemsToScrape": <int> }
  - endpoint : POST /v2/acts/{actor}/run-sync-get-dataset-items (run synchrone)
  - coût annoncé ≈ 1,2 $ / 1000 items (≈ 0,0012 $/item)

Secret : la clé Apify est lue dans l'environnement (`APIFY_TOKEN`), JAMAIS en dur.
Charger via .env avant de lancer :  set -a; . ./.env; set +a
Si `APIFY_TOKEN` est absent → log + renvoie [] (non-fatal).

Interface : async def search(criteres: dict) -> list[dict]

POST-FILTRE par code_postal[:2] ∈ departements (sécurité 0 fuite — l'acteur peut
retomber sur une recherche nationale si l'URL/geo-id est inattendu).
"""
import asyncio
import os
import re

import httpx

APIFY_ACTOR = "azzouzana~pap-fr-mass-products-scraper-by-search-url"
APIFY_ENDPOINT = (
    f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items"
)
SITE_BASE = "https://www.pap.fr"

# PAP encode chaque département par un slug + un identifiant géographique « g{ID} ».
# Format URL : https://www.pap.fr/annonce/vente-maisons-{slug}-{dept}-g{ID}
# (le slug inclut déjà le numéro de dept). Geo-ids vérifiés (mai 2026) — sans le bon
# g{ID}, PAP retombe sur une recherche nationale, d'où le post-filtre en sécurité.
DEPT_GEO = {
    "72": ("sarthe-72", "g436"),
    "28": ("eure-et-loir-28", "g392"),
    "45": ("loiret-45", "g409"),
    "89": ("yonne-89", "g453"),
    "49": ("maine-et-loire-49", "g413"),
    "37": ("indre-et-loire-37", "g401"),
    "36": ("indre-36", "g400"),
    "18": ("cher-18", "g381"),
    "58": ("nievre-58", "g422"),
    "41": ("loir-et-cher-41", "g405"),
    "53": ("mayenne-53", "g417"),
}

# Par défaut : maxItems modéré par département (maîtrise du coût Apify).
MAX_ITEMS_PER_DEPT = 60

_CITY_CP_RE = re.compile(r"^(.*?)\s*\((\d{5})\)\s*$")
_SURFACE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m[²2]")
_TERRAIN_RE = re.compile(r"[Tt]errain\s+([\d.\s  ]+)\s*m[²2]")
_PIECES_RE = re.compile(r"(\d+)\s*pi[èe]ce", re.IGNORECASE)


def _build_search_url(dept: str) -> str | None:
    entry = DEPT_GEO.get(dept)
    if not entry:
        return None
    slug, geo = entry
    return f"{SITE_BASE}/annonce/vente-maisons-{slug}-{geo}"


def _parse_int(raw) -> int | None:
    """'134.000 €' / '120 m²' → 134000 / 120 (retire séparateurs de milliers)."""
    if raw is None:
        return None
    digits = re.sub(r"[^\d]", "", str(raw))
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _parse_item(item: dict, dept: str) -> dict | None:
    if not isinstance(item, dict):
        return None
    # L'acteur peut injecter un objet de message (ex. rate-limit) sans champ métier.
    if "titre" not in item or "prix" not in item:
        return None

    prix = _parse_int(item.get("prix"))
    if not prix or prix < 5000:
        return None

    # Ville + code postal depuis le titre "Ville (CP)".
    titre_raw = (item.get("titre") or "").strip()
    ville, cp = "", ""
    m = _CITY_CP_RE.match(titre_raw)
    if m:
        ville = m.group(1).strip()
        cp = m.group(2)

    carac = item.get("caracteristiques") or ""

    surface = None
    sm = _SURFACE_RE.search(carac)
    if sm:
        try:
            surface = float(sm.group(1).replace(",", "."))
        except ValueError:
            surface = None

    surface_terrain = None
    tm = _TERRAIN_RE.search(carac)
    if tm:
        surface_terrain = _parse_int(tm.group(1))

    pieces = item.get("nb_pieces")
    if not isinstance(pieces, int):
        pm = _PIECES_RE.search(carac)
        pieces = int(pm.group(1)) if pm else None

    chambres = item.get("nb_chambres_max")
    chambres = chambres if isinstance(chambres, int) else None

    # DPE
    dpe = None
    ce = item.get("classe_energie")
    if isinstance(ce, dict) and ce.get("lettre"):
        dpe = str(ce["lettre"]).upper()

    # Coords GPS (marker)
    lat = lng = None
    marker = item.get("marker")
    if isinstance(marker, dict):
        try:
            lat = float(marker["lat"]) if marker.get("lat") is not None else None
            lng = float(marker["lng"]) if marker.get("lng") is not None else None
        except (TypeError, ValueError):
            lat = lng = None

    # Photos
    photos_raw = item.get("photos") or []
    if isinstance(photos_raw, list):
        photos = [p for p in photos_raw if isinstance(p, str) and p.startswith("http")][:10]
    else:
        photos = []

    # URL
    url = item.get("url") or ""
    if url and not url.startswith("http"):
        url = SITE_BASE + url
    if not url:
        ann_id = item.get("id")
        url = f"{SITE_BASE}/annonces/-r{ann_id}" if ann_id else SITE_BASE

    type_bien = (item.get("typebien_slug") or item.get("typebien") or "maison").lower()
    if carac and ville:
        titre = f"{type_bien.capitalize()} {ville} — {carac}"[:150]
    else:
        titre = titre_raw or f"{type_bien.capitalize()} — {ville} ({cp})"

    return {
        "source": "pap",
        "url": url,
        "id_annonce": str(item.get("id")) if item.get("id") is not None else None,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": (item.get("texte") or "")[:500],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "latitude": lat,
        "longitude": lng,
        "surface": surface,
        "surface_terrain": float(surface_terrain) if surface_terrain else None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": float(prix),
        "dpe": dpe,
        "photos": photos,
        "agence": "PAP (particulier)",
    }


async def _scrape_dept(
    client: httpx.AsyncClient, token: str, dept: str, max_items: int
) -> list[dict]:
    url = _build_search_url(dept)
    if not url:
        print(f"[PAP] pas de geo-id pour dept {dept}, skip")
        return []

    payload = {"startUrl": url, "maxItemsToScrape": max_items}
    try:
        r = await client.post(
            APIFY_ENDPOINT,
            params={"token": token},
            json=payload,
        )
    except Exception as e:
        print(f"[PAP] erreur réseau dept {dept}: {e}")
        return []

    if r.status_code == 402:
        print(f"[PAP] dept {dept}: quota/crédits Apify épuisés (402)")
        return []
    if r.status_code == 429:
        print(f"[PAP] dept {dept}: rate-limit Apify (429)")
        return []
    if r.status_code >= 400:
        print(f"[PAP] dept {dept}: HTTP {r.status_code} — {r.text[:120]}")
        return []

    try:
        data = r.json()
    except Exception as e:
        print(f"[PAP] dept {dept}: JSON invalide: {e}")
        return []

    if not isinstance(data, list):
        print(f"[PAP] dept {dept}: réponse inattendue (pas une liste)")
        return []

    # Détection d'un message de service (rate-limit free tier, etc.)
    if (
        len(data) == 1
        and isinstance(data[0], dict)
        and "message" in data[0]
        and "titre" not in data[0]
    ):
        print(f"[PAP] dept {dept}: acteur a renvoyé un message: {data[0]['message'][:120]}")
        return []

    biens = []
    leaked = 0
    for item in data:
        bien = _parse_item(item, dept)
        if not bien:
            continue
        # POST-FILTRE 0 fuite : code_postal[:2] doit matcher le département cible.
        cp = bien.get("code_postal") or ""
        if cp[:2] != dept:
            leaked += 1
            continue
        biens.append(bien)

    extra = f" ({leaked} hors-dept filtrés)" if leaked else ""
    print(
        f"[PAP] dept {dept}: {len(biens)} annonces (sur {len(data)} items)"
        f"{extra} — coût ≈ {len(data) * 0.0012:.4f}$"
    )
    return biens


async def search(criteres: dict) -> list[dict]:
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        print("[PAP] APIFY_TOKEN manquant — source désactivée")
        return []

    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    departements = [d for d in departements if d in DEPT_GEO]
    if not departements:
        print("[PAP] aucun département couvert par PAP dans les critères")
        return []

    prix_max = criteres.get("prix_max")
    prix_min = criteres.get("prix_min")
    surface_min = criteres.get("surface_min")
    max_items = criteres.get("pap_max_items", MAX_ITEMS_PER_DEPT)

    results: list[dict] = []
    # Acteur synchrone, runs longs : timeout généreux, séquentiel (évite le rate-limit).
    timeout = httpx.Timeout(connect=20, read=600, write=20, pool=600)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for dept in departements:
            biens = await _scrape_dept(client, token, dept, max_items)
            for b in biens:
                p = b.get("prix") or 0
                s = b.get("surface") or 0
                if prix_max and p > prix_max:
                    continue
                if prix_min and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue
                results.append(b)
            await asyncio.sleep(0.5)

    print(f"[PAP] total: {len(results)} annonces")
    return results


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    async def _test():
        # Test minimal maîtrise du coût : 1 seul département, maxItems faible.
        criteres = {
            "departements": [72],
            "pap_max_items": 20,
        }
        biens = await search(criteres)
        print(f"\n--- {len(biens)} annonces PAP (dept 72) ---")
        for b in biens[:8]:
            print(
                f"  {b['ville']} ({b['code_postal']}) — {int(b['prix'])}€ — "
                f"{b['surface']}m² — terrain {b['surface_terrain']} — "
                f"{b['pieces']}p — DPE {b['dpe']} — {b['url']}"
            )

    asyncio.run(_test())

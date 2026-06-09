"""scrapers/beemmo.py — Beemmo (néo-agence en ligne, forfait fixe sans commission)

Méthode : scrape_simple (httpx) — SSR Next.js
URL : /annonces  → renvoie le catalogue NATIONAL (pas de filtre département
      dans l'URL). Les annonces sont injectées en JSON propre dans
      <script id="__NEXT_DATA__"> → props.pageProps.annonces_props (liste).
      → Aucun filtre serveur par département → POST-FILTRE Python strict sur
        code_postal[:2] (objectif 0 fuite).

Champs JSON utiles par annonce :
  - postal_code, city            → code postal / ville
  - price                        → prix (€)
  - surface_living_space         → surface habitable
  - surface_land                 → surface terrain
  - rooms, bedrooms              → pièces / chambres
  - product_type                 → 1=appartement, 2=maison
  - product_offer_type           → 1=vente (on ne garde que la vente)
  - epc_energy / epc_climate     → DPE (lettre déduite du barème conso)
  - images / images_public       → URLs photos
  - product_ref                  → référence (id_annonce)
  - website_url_rewriting        → slug → URL détail beemmo.fr/annonces/{slug}
  - title / details_listing      → titre / description

Distribution observée (2026-06) : 06(84), 75(33), 93(27), 92(15), 94(13),
83(3), 95(2) — 0 bien dans la zone cible (72/28/45/89/49/37/36/18/58/41/53).
Scraper fonctionnel mais sans stock zone → actif:false dans sources.yaml.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx

BASE_URL = "https://beemmo.fr"
LISTING_URL = f"{BASE_URL}/annonces"
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# product_type → libellé
_PRODUCT_TYPE = {1: "appartement", 2: "maison"}

# Barème DPE (lettre) à partir de la conso énergie primaire (kWh/m²/an)
_DPE_BANDS = [
    (70, "A"), (110, "B"), (180, "C"), (250, "D"),
    (330, "E"), (420, "F"),
]


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
    ) as client:
        try:
            annonces = await _fetch_annonces(client)
        except Exception as e:
            print(f"[Beemmo] Erreur fetch /annonces: {e}")
            return results

        print(f"[Beemmo] {len(annonces)} annonces nationales récupérées")

        seen: set[str] = set()
        per_dept: dict[str, int] = {}
        for a in annonces:
            bien = _parse_annonce(a, departements)
            if not bien:
                continue

            # Post-filtre département STRICT (aucun filtre serveur)
            cp = bien["code_postal"]
            if not cp or cp[:2] not in departements:
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
            per_dept[cp[:2]] = per_dept.get(cp[:2], 0) + 1

    for dept in sorted(departements):
        print(f"[Beemmo] Dept {dept}: {per_dept.get(dept, 0)} annonces")
    return results


async def _fetch_annonces(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(LISTING_URL)
    if r.status_code != 200:
        print(f"[Beemmo] status {r.status_code} sur {LISTING_URL}")
        return []
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S
    )
    if not m:
        print("[Beemmo] __NEXT_DATA__ introuvable")
        return []
    data = json.loads(m.group(1))
    ann = data.get("props", {}).get("pageProps", {}).get("annonces_props", [])
    return ann if isinstance(ann, list) else []


def _parse_annonce(a: dict, departements: set[str]) -> dict | None:
    # On ne garde que la vente
    if a.get("product_offer_type") != 1:
        return None

    cp = str(a.get("postal_code") or "").strip()
    if not cp or len(cp) < 2:
        return None
    # Coupe court avant tout parsing inutile si hors zone
    if cp[:2] not in departements:
        return None

    dept = cp[:2]
    ville = str(a.get("city") or "").strip()

    type_bien = _PRODUCT_TYPE.get(a.get("product_type"), "bien")

    ref = str(a.get("product_ref") or "").strip()
    oid = str(a.get("_id") or "").strip()
    id_annonce = ref or oid
    if not id_annonce:
        return None

    slug = str(a.get("website_url_rewriting") or "").strip()
    url = f"{BASE_URL}/annonces/{slug}" if slug else LISTING_URL

    titre = str(a.get("title") or "").strip()
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    description = str(a.get("details_listing") or a.get("details") or "")
    description = re.sub(r"<[^>]+>", " ", description)
    description = re.sub(r"\s+", " ", description).strip()

    prix = _num(a.get("price"))
    surface = _num(a.get("surface_living_space"))
    surface_terrain = _num(a.get("surface_land"))
    pieces = _int(a.get("rooms"))
    chambres = _int(a.get("bedrooms"))

    dpe = _dpe_letter(a.get("epc_energy"))

    photos: list[str] = []
    for src in (a.get("images") or a.get("images_public") or []):
        if isinstance(src, str) and src.startswith("http"):
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "beemmo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Beemmo",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _num(v) -> float | None:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    try:
        i = int(v)
        return i if i > 0 else None
    except (TypeError, ValueError):
        return None


def _dpe_letter(energy) -> str | None:
    val = _num(energy)
    if val is None:
        return None
    for limit, letter in _DPE_BANDS:
        if val <= limit:
            return letter
    return "G"


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
    print(f"\nTotal Beemmo: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

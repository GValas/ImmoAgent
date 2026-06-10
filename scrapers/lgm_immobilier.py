"""scrapers/lgm_immobilier.py — LGM Immobilier (Groupement des Mandataires en Immobilier)

Méthode : api_inoff (httpx) — le site public www.lgm-immobilier.fr/nos-biens est un
front Next.js rendu côté client (CSR : le HTML brut ne contient aucune annonce). Les
données sont servies par une API JSON publique SweepBright, sans authentification :

    GET https://app.lgm-immobilier.fr/sb/sales?page={N}
    → réponse paginée Laravel : {current_page, last_page, total, per_page(=18), data:[...]}

Pas de filtre département côté serveur → on crawle l'inventaire NATIONAL complet
(~104 pages, ~1900 biens, fortement implanté sud Gard/Hérault) puis POST-FILTRE
STRICT sur location.postal_code[:2] ∈ départements cibles. La zone Val-de-Loire est
bien fournie (41 et 28 surtout, un peu 45/49/37) → 0 fuite garantie.

Champs d'un enregistrement (extraits) :
  - id / property_id / slug          → détail public : /biens/{slug}
  - title, description, amount (prix)
  - type (house/apartment/land/commercial), sub_type
  - negociation : "sale" | "let"     → on ne garde QUE "sale"
  - bedrooms, living_rooms, kitchens, bathrooms, toilets (→ pièces estimées)
  - sizes (JSON str) : liveable_area.size (surface hab), plot_area.size (terrain)
  - location (JSON str) : city, postal_code, geo{lat,lng}
  - legal (JSON str) : energy.dpe (A..G)
  - images (JSON str) : {image_1: "uuid.jpeg", ...} → base S3 sweepbright-images

Types conservés : house (+ sous-types maison). On exclut apartment / land / commercial.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx

API_URL = "https://app.lgm-immobilier.fr/sb/sales"
WEBSITE_BASE = "https://www.lgm-immobilier.fr"
IMG_BASE = "https://sweepbright-images.s3.eu-west-3.amazonaws.com"
MAX_PAGES = 110  # garde-fou (last_page observé ~104)
PHOTOS_PER_CARD = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Origin": WEBSITE_BASE,
    "Referer": WEBSITE_BASE + "/",
}

# Types "house" sous lesquels SweepBright range les maisons/propriétés.
# On part du type principal == "house" (les apartment/land/commercial sont exclus).


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    results: list[dict] = []
    seen: set = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
    ) as client:
        page = 1
        last_page = MAX_PAGES
        while page <= min(last_page, MAX_PAGES):
            try:
                r = await client.get(API_URL, params={"page": page})
            except Exception as e:
                print(f"[LGM] Erreur réseau page {page}: {e}")
                break
            if r.status_code != 200:
                print(f"[LGM] Page {page} → HTTP {r.status_code}, arrêt")
                break
            try:
                payload = r.json()
            except Exception:
                break

            last_page = payload.get("last_page", last_page)
            data = payload.get("data") or []
            if not data:
                break

            for rec in data:
                try:
                    bien = _parse_record(rec, departements)
                except Exception:
                    continue
                if not bien:
                    continue
                if bien["id_annonce"] in seen:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                seen.add(bien["id_annonce"])
                results.append(bien)

            page += 1
            await asyncio.sleep(0.5)

    # Récap par département (utile au debug, 0 fuite attendu)
    by_dept: dict[str, int] = {}
    for b in results:
        d = b["code_postal"][:2] if b["code_postal"] else "??"
        by_dept[d] = by_dept.get(d, 0) + 1
    print(f"[LGM] {len(results)} annonces (maisons) — par dept : {by_dept}")

    return results


def _loads(value):
    """Décode un champ qui peut être une chaîne JSON ou déjà un dict/None."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {}
    return value or {}


def _parse_record(rec: dict, departements: set) -> dict | None:
    # On ne garde que les ventes de maisons
    if rec.get("negociation") not in (None, "sale"):
        return None
    if rec.get("type") != "house":
        return None

    location = _loads(rec.get("location"))
    cp = str(location.get("postal_code") or "").strip()
    dept = cp[:2] if len(cp) >= 2 else ""

    # POST-FILTRE STRICT département (pas de filtre serveur disponible)
    if dept not in departements:
        return None

    ville = (location.get("city") or "").strip()

    slug = rec.get("slug") or ""
    url = f"{WEBSITE_BASE}/biens/{slug}" if slug else WEBSITE_BASE + "/nos-biens"

    prix = rec.get("amount")
    try:
        prix = float(prix) if prix is not None else None
    except (TypeError, ValueError):
        prix = None

    # Surface habitable / terrain depuis `sizes`
    sizes = _loads(rec.get("sizes"))
    surface = _size_value(sizes.get("liveable_area"))
    if surface is None:
        ls = rec.get("living_space")
        if ls:
            surface = float(ls)
    surface_terrain = _size_value(sizes.get("plot_area"))
    if surface_terrain is None:
        la = rec.get("land_area")
        if la:
            surface_terrain = float(la)

    # Pièces : pas de champ direct → estimation chambres + salons (mini = chambres)
    bedrooms = _int(rec.get("bedrooms"))
    living_rooms = _int(rec.get("living_rooms"))
    pieces = None
    if bedrooms is not None or living_rooms is not None:
        pieces = (bedrooms or 0) + (living_rooms or 0) or None

    # DPE
    legal = _loads(rec.get("legal"))
    dpe = None
    energy = legal.get("energy") if isinstance(legal, dict) else None
    if isinstance(energy, dict):
        d = energy.get("dpe")
        if isinstance(d, str) and d.strip().upper() in {
            "A", "B", "C", "D", "E", "F", "G",
        }:
            dpe = d.strip().upper()

    # Photos
    images = _loads(rec.get("images"))
    photos = []
    if isinstance(images, dict):
        keyed = [
            (int(m.group(1)), v)
            for k, v in images.items()
            if (m := re.match(r"image_(\d+)$", k)) and v
        ]
        for _, fname in sorted(keyed):
            fname = str(fname).lstrip("/")
            photos.append(f"{IMG_BASE}/{fname}")
    photos = photos[:PHOTOS_PER_CARD]

    titre = (rec.get("title") or "").strip() or f"Maison {ville}".strip()
    description = (rec.get("description") or "").strip()

    type_bien = "maison"
    sub = rec.get("sub_type")
    if isinstance(sub, str) and sub:
        type_bien = sub.replace("_", " ").strip() or "maison"

    agent = " ".join(
        x for x in [rec.get("firstname"), rec.get("lastname")] if x
    ).strip()
    agence = f"LGM Immobilier{' — ' + agent if agent else ''}"

    return {
        "source": "lgm_immobilier",
        "url": url,
        "id_annonce": str(rec.get("id") or rec.get("property_id") or slug or url),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": bedrooms,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": agence[:120],
    }


def _size_value(block) -> float | None:
    if isinstance(block, dict):
        val = block.get("size")
        try:
            f = float(val)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None
    return None


def _int(value) -> int | None:
    try:
        i = int(value)
        return i
    except (TypeError, ValueError):
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
    print(f"\nTotal LGM Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — DPE {b.get('dpe') or '?'}"
            f" — {b['type_bien']} — {b['ville']}"
        )

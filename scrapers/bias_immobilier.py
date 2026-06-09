"""scrapers/bias_immobilier.py — BIAS Immobilier (réseau ~22 agences Normandie)

Méthode : api_inoff (httpx) — API WordPress admin-ajax (back-end Netty.immo)
Site     : https://biasimmobilier.fr (WordPress + Vue.js, listing rendu côté client).

Le HTML de /resultat-acheter ne contient pas l'inventaire (Vue le charge en JS).
Le front appelle une action AJAX WordPress qui renvoie TOUT l'inventaire en un
seul POST JSON :

    POST https://biasimmobilier.fr/wp-admin/admin-ajax.php
    data: action=ajaxachat2 & type_annonce=Vente
        (+ filtres optionnels : type_bien, prix_min, prix_max, surface,
           nb_pieces, nb_chambres, ville/lat/lng/rayon — non utilisés ici :
           on récupère tout puis on filtre en Python)

Réponse : liste d'objets avec, entre autres :
    id, reference, titre, description, type_bien, sous_type_bien,
    code_postal, ville, prix, surface_habitable, surface_terrain,
    nb_pieces, nb_chambres, bilan_energie (DPE), agence, slug (= URL détail),
    images (string CSV d'URLs), lat, lng.

Filtre département : AUCUN filtre serveur fiable par dept → on récupère tout
le réseau et on POST-FILTRE STRICT sur code_postal[:2].
⚠️ Le réseau dépasse 76/27 : l'inventaire contient aussi le Calvados (14) et
   quelques biens dans l'Orne (61). Le post-filtre CP[:2] est donc INDISPENSABLE
   pour garantir 0 fuite hors-zone.

Couverture observée (2026-06-08) : 27=315, 76=197, 14=145, 61=2 (Normandie).
→ AUCUN des départements cibles Val-de-Loire/Ouest (72, 28, 45, 89, 49, 37...)
   n'est couvert : ce scraper renvoie 0 bien sur la zone actuelle, mais reste
   fonctionnel (réactiver si la zone cible s'étend à la Normandie).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx

BASE_URL = "https://biasimmobilier.fr"
AJAX_URL = f"{BASE_URL}/wp-admin/admin-ajax.php"
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/resultat-acheter",
}

# Types de bien à conserver (maisons / propriétés). On exclut appartements,
# terrains, immeubles, locaux pro, stationnements.
_KEEP_TYPE = re.compile(
    r"maison|propri|villa|ferme|long[eè]re|manoir|chateau|château|moulin|"
    r"demeure|domaine|mas|corps[- ]de[- ]ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|immeuble|local|commerc|pro|stationnement|parking|"
    r"garage|bureau|fonds|viager",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=90
    ) as client:
        try:
            r = await client.post(
                AJAX_URL, data={"action": "ajaxachat2", "type_annonce": "Vente"}
            )
        except Exception as e:
            print(f"[BiasImmo] Erreur requête API : {e}")
            return []

        if r.status_code != 200:
            print(f"[BiasImmo] HTTP {r.status_code} sur l'API")
            return []

        try:
            data = r.json()
        except Exception as e:
            print(f"[BiasImmo] Réponse non-JSON : {e}")
            return []

    if not isinstance(data, list):
        print(f"[BiasImmo] Format inattendu ({type(data).__name__})")
        return []

    print(f"[BiasImmo] {len(data)} biens bruts dans l'inventaire réseau")

    results: list[dict] = []
    seen_ids: set[str] = set()
    par_dept: dict[str, int] = {}

    for raw in data:
        try:
            bien = _parse_bien(raw)
        except Exception:
            continue
        if not bien:
            continue

        cp = bien["code_postal"]
        # POST-FILTRE DÉPARTEMENT STRICT — 0 fuite hors-zone
        if not cp or cp[:2] not in departements:
            continue

        aid = bien["id_annonce"]
        if aid in seen_ids:
            continue

        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        seen_ids.add(aid)
        bien["departement"] = cp[:2]
        results.append(bien)
        par_dept[cp[:2]] = par_dept.get(cp[:2], 0) + 1

    for dept in departements:
        print(f"[BiasImmo] Dept {dept}: {par_dept.get(dept, 0)} annonces")

    return results


def _parse_bien(raw: dict) -> dict | None:
    type_bien_raw = (raw.get("type_bien") or "").strip()
    sous_type = (raw.get("sous_type_bien") or "").strip()
    type_label = f"{type_bien_raw} {sous_type}".strip()

    # Filtre type : on ne garde que les maisons / propriétés
    if _EXCLUDE_TYPE.search(type_label) and not _KEEP_TYPE.search(type_label):
        return None
    if not _KEEP_TYPE.search(type_label):
        return None
    type_bien = (sous_type or type_bien_raw or "maison").lower()

    cp = re.sub(r"\D", "", str(raw.get("code_postal") or ""))[:5]
    ville = (raw.get("ville") or "").strip()

    slug = (raw.get("slug") or "").strip()
    if slug.startswith("http"):
        url = slug
    elif slug:
        url = BASE_URL + ("/" + slug.lstrip("/"))
    else:
        url = BASE_URL + "/resultat-acheter"
    id_annonce = str(raw.get("id") or raw.get("reference") or url)
    if raw.get("id"):
        url = f"{url}?idBien={raw['id']}"

    titre = (raw.get("titre") or "").strip() or f"{type_bien.title()} {ville}".strip()
    description = re.sub(r"<[^>]+>", " ", raw.get("description") or "")
    description = re.sub(r"\s+", " ", description).strip()

    prix = _to_float(raw.get("prix"))
    surface = _to_float(raw.get("surface_habitable"))
    surface_terrain = _to_float(raw.get("surface_terrain"))
    pieces = _to_int(raw.get("nb_pieces"))
    chambres = _to_int(raw.get("nb_chambres"))

    dpe = (raw.get("bilan_energie") or "").strip().upper() or None
    if dpe and dpe not in {"A", "B", "C", "D", "E", "F", "G"}:
        dpe = None

    # images : chaîne CSV d'URLs
    photos: list[str] = []
    imgs = raw.get("images")
    if isinstance(imgs, str):
        photos = [u.strip() for u in imgs.split(",") if u.strip().startswith("http")]
    elif isinstance(imgs, list):
        photos = [u for u in imgs if isinstance(u, str) and u.startswith("http")]
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "bias_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": cp[:2] if cp else "",
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": (raw.get("agence") or "BIAS Immobilier").strip()[:120],
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _to_float(val) -> float | None:
    if val is None:
        return None
    s = re.sub(r"[^\d.,]", "", str(val)).replace(",", ".")
    try:
        f = float(s)
        return f if f > 0 else None
    except ValueError:
        return None


def _to_int(val) -> int | None:
    f = _to_float(val)
    return int(f) if f is not None else None


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
    print(f"\nTotal BIAS Immobilier: {len(biens)} annonces")
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

"""scrapers/immobiliere_normandie.py — L'Immobilière de Normandie (réseau ~9 agences)

Méthode : api_inoff (httpx) — endpoint JSON DMXzone/AppConnect.
Front Wappler/DMXzone : le HTML de listebiens.php ne contient QUE les filtres ;
les cartes sont injectées côté client via des bindings dmx-on/dmx-show qui
appellent un serveraction JSON. Endpoint localisé :

    GET dmxConnect/api/listeaffaires.php?typetransaction=VENTE
    → {"selectionaffaires": [ {...}, ... ]}   (~330 biens, SANS pagination)

⚠️ L'endpoint IGNORE les paramètres de filtre géographique (biencp/secteur/region) :
il renvoie TOUT le portefeuille national en un seul appel. Le réseau est implanté
sur l'Orne (61) et l'Eure (27) avec quelques biens débordant (28, 72, 91, 92…).
→ Pas de filtre département serveur fiable. Filtre CÔTÉ CLIENT strict sur
   BienCP[:2] in departements (garde-fou « 0 fuite »).

Champs JSON utilisés :
  Code         → id interne (clé de la fiche détail : ficheaffaire.php?code={Code})
  NMandat      → numéro de mandat (id_annonce lisible)
  RefAnnonce   → référence publique (fallback id)
  BienCP       → code postal (filtre dept)
  BienVille    → ville
  PrixMandatEuro → prix de vente en € (Prix est souvent 0)
  TypeAffaire  → "maison", "propriete/demeure de caractère", "appartement"…
  TextePub / TexteInternet1 → description
  NbPieces, NbChambres, SurfHab, SurfTerrain
  DPELettre    → classe énergie
  CodeAgence   → préfixe CDN photos (ex "norm")
  NumPhoto, NumPhoto1..NumPhoto20 → indices des photos disponibles

Photos : https://www.selection-immo.com/Photos/{CodeAgence}/Photos/{Code}-{N}.jpg
  (et {Code}.jpg pour la principale). Le CDN selection-immo.com négocie en DH
  faible → SSL "DH_KEY_TOO_SMALL" sous httpx strict ; les URLs restent valides
  (on ne télécharge pas ici, on stocke les liens).

Type de bien : on ne garde que maisons / propriétés / demeures (pas appartement,
terrain, local, fonds…).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx

BASE_URL = "https://www.immobiliere-normandie.com"
API_URL = f"{BASE_URL}/dmxConnect/api/listeaffaires.php"
PHOTO_CDN = "https://www.selection-immo.com/Photos"
PHOTOS_PER_CARD = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Referer": f"{BASE_URL}/listebiens.php?typetransaction=VENTE",
    "X-Requested-With": "XMLHttpRequest",
}

# Types de bien (TypeAffaire / CritC1) à conserver
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps de ferme|caractère|"
    r"presbyt|haras",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|viager|bureaux|entrep",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
    ) as client:
        try:
            r = await client.get(API_URL, params={"typetransaction": "VENTE"})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[ImmoNormandie] Erreur API: {e}")
            return results

        affaires = data.get("selectionaffaires", []) if isinstance(data, dict) else []
        print(f"[ImmoNormandie] {len(affaires)} biens bruts (portefeuille national)")

        for aff in affaires:
            try:
                bien = _parse_affaire(aff, departements)
            except Exception:
                continue
            if not bien:
                continue

            # Filtre département STRICT (l'API ne filtre pas) → 0 fuite
            cp = bien["code_postal"] or ""
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
            results.append(bien)

    # Récap par département pour le log
    from collections import Counter
    distrib = Counter(b["code_postal"][:2] for b in results if b["code_postal"])
    print(f"[ImmoNormandie] {len(results)} biens retenus — {dict(distrib)}")
    return results


def _parse_affaire(aff: dict, departements: list[str]) -> dict | None:
    type_raw = (aff.get("TypeAffaire") or aff.get("CritC1") or "").strip()
    if _EXCLUDE_TYPE.search(type_raw) and not _KEEP_TYPE.search(type_raw):
        return None
    if not _KEEP_TYPE.search(type_raw):
        return None
    type_bien = type_raw.split("/")[0].strip().lower() or "maison"

    cp = (aff.get("BienCP") or "").strip()
    if not cp or cp[:2] not in departements:
        return None
    dept = cp[:2]

    ville = _titlecase(aff.get("BienVille") or aff.get("Secteur") or "")

    code = str(aff.get("Code") or "").strip()
    nmandat = str(aff.get("NMandat") or "").strip()
    ref = str(aff.get("RefAnnonce") or "").strip()
    id_annonce = nmandat or ref or code
    if not code:
        return None
    url = f"{BASE_URL}/ficheaffaire.php?code={code}"

    prix = _to_float(aff.get("PrixMandatEuro"))
    if not prix:
        prix = _to_float(aff.get("Prix")) or None

    surface = _to_float(aff.get("SurfHab"))
    surface_terrain = _to_float(aff.get("SurfTerrain"))
    pieces = _to_int(aff.get("NbPieces"))
    chambres = _to_int(aff.get("NbChambres"))

    description = (
        aff.get("TextePub")
        or aff.get("TexteInternet1")
        or ""
    ).strip()

    dpe = (aff.get("DPELettre") or "").strip().upper() or None
    if dpe in ("", "N", "NS", "NC", "VI"):  # non soumis / non communiqué
        dpe = None

    titre = f"{type_bien.capitalize()} {ville}".strip()
    if surface:
        titre += f" {int(surface)} m²"

    photos = _build_photos(aff, code)

    return {
        "source": "immobiliere_normandie",
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
        "agence": "L'Immobilière de Normandie",
    }


def _build_photos(aff: dict, code: str) -> list[str]:
    agence = (aff.get("CodeAgence") or "").strip()
    if not agence or not code:
        return []
    base = f"{PHOTO_CDN}/{agence}/Photos/{code}"
    photos: list[str] = []
    seen: set[int] = set()

    # Photo principale = {code}.jpg
    photos.append(f"{base}.jpg")

    # Indices réels dans NumPhoto1..NumPhoto20 (0 = absente)
    for i in range(1, 21):
        n = _to_int(aff.get(f"NumPhoto{i}"))
        if n and n > 0 and n not in seen:
            seen.add(n)
            photos.append(f"{base}-{n}.jpg")
        if len(photos) >= PHOTOS_PER_CARD:
            break
    return photos[:PHOTOS_PER_CARD]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _to_float(v) -> float | None:
    if v is None:
        return None
    s = re.sub(r"[^\d.]", "", str(v).replace(",", "."))
    try:
        f = float(s)
        return f if f > 0 else None
    except ValueError:
        return None


def _to_int(v) -> int | None:
    f = _to_float(v)
    return int(f) if f is not None else None


def _titlecase(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if text.isupper() or text.islower():
        return text.title()
    return text


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
    print(f"\nTotal Immobilière de Normandie: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']} — {len(b['photos'])} photos"
        )

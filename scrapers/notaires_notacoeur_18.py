"""scrapers/notaires_notacoeur_18.py — SAS NOTACOEUR (office notarial de Bourges, Cher 18)

Méthode : api_inoff (httpx) — JSON
Le front https://notacoeur-bourges.notaires.fr/annonces-immobilieres/ est un
Nuxt (CSR/SSG) : le HTML brut ne contient AUCUNE annonce (payload.js vide,
state.annonces=[]). Les annonces sont chargées côté client depuis l'API
officielle immobilier.notaires.fr, filtrée par le code office (crpcen) de
l'étude, exposé dans la page :
    https://www.immobilier.notaires.fr/fr/annonces-immobilieres-liste?crpcen=18005

On interroge donc directement cette API en la scopant à l'office 18005 :
    GET /pub-services/inotr-www-annonces/v1/annonces?crpcen=18005&typeTransactions=VENTE&...
→ ~26 annonces, TOUTES dans le Cher (18) : Bourges, Saint-Doulchard,
  Farges-en-Septaine… Filtre département CÔTÉ SERVEUR (l'office ne vend que
  dans son ressort), doublé d'un post-filtre strict code_postal[:2].

Distinct de scrapers/immobilier_notaires.py (qui interroge l'API par
département, national) : ici on cible le STOCK PROPRE de l'étude NOTACOEUR,
département 18 sous-pourvu par les autres sources.

Champs API (annonceResumeDto[]) : annonceId, reference, typeBien (MAI/APP/IMM/GAR…),
prixAffiche, surface, surfaceTerrain, nbPieces, communeNom, codePostal,
inseeDepartement, urlDetailAnnonceFr, urlPhotoPrincipale, descriptionFr,
dateRealisationDpe, dateCreation.

On ne garde que maisons / villas / châteaux / manoirs (types ruraux),
on exclut appartements / garages / immeubles / parkings.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio

import httpx

# Code office (crpcen) de l'étude NOTACOEUR à Bourges
CRPCEN = "18005"
# Département(s) couvert(s) par cet office — sécurité post-filtre
OFFICE_DEPTS = {"18"}

API_URL = (
    "https://www.immobilier.notaires.fr/pub-services/inotr-www-annonces/v1/annonces"
)
MAX_PAGES = 6  # 24/page ; l'office a ~30 annonces, large marge

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Referer": "https://notacoeur-bourges.notaires.fr/",
}

# Types de bien (codes API) à conserver : maisons / villas / châteaux / manoirs.
_KEEP_TYPES = {"MAI", "MAIS", "VIL", "CHA", "MAN", "PRO", "DEM", "FER"}
# Types explicitement écartés (appartement, immeuble, garage, terrain, fonds, parking…)
_EXCLUDE_TYPES = {"APP", "IMM", "GAR", "PAR", "TER", "FON", "LOC", "BUR", "COM"}

_TYPE_LABEL = {
    "MAI": "maison",
    "MAIS": "maison",
    "VIL": "villa",
    "CHA": "château",
    "MAN": "manoir",
    "PRO": "propriété",
    "DEM": "demeure",
    "FER": "ferme",
}


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Si aucun département cible ne recoupe le ressort de l'office, rien à faire
    if departements and not (departements & OFFICE_DEPTS):
        print(
            f"[NotacoeurBourges] Aucun dept cible dans le ressort de l'office "
            f"({OFFICE_DEPTS}) — skip"
        )
        return []

    results: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            params = {
                "offset": str((page - 1) * 24),
                "page": str(page),
                "parPage": "24",
                "perimetre": "0",
                "crpcen": CRPCEN,
                "typeTransactions": "VENTE",
            }
            try:
                r = await client.get(API_URL, params=params)
            except Exception as e:
                print(f"[NotacoeurBourges] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                print(f"[NotacoeurBourges] HTTP {r.status_code} page {page}")
                break

            try:
                data = r.json()
            except Exception:
                print(f"[NotacoeurBourges] JSON invalide page {page}")
                break

            ads = data.get("annonceResumeDto", []) or []
            if not ads:
                break

            for ad in ads:
                bien = _parse_ad(ad)
                if not bien:
                    continue

                # Post-filtre département STRICT (0 fuite hors-zone)
                cp = bien["code_postal"]
                dept = cp[:2] if cp else (bien.get("departement") or "")[:2]
                if dept not in OFFICE_DEPTS:
                    continue
                if departements and dept not in departements:
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

            nb_pages = data.get("nbPages", 1) or 1
            if page >= nb_pages:
                break
            await asyncio.sleep(0.5)

    print(f"[NotacoeurBourges] {len(results)} annonces (office {CRPCEN}, dept 18)")
    return results


def _parse_ad(ad: dict) -> dict | None:
    try:
        type_code = (ad.get("typeBien") or "").upper()
        if type_code in _EXCLUDE_TYPES:
            return None
        if type_code not in _KEEP_TYPES:
            return None

        url = ad.get("urlDetailAnnonceFr", "")
        if not url:
            return None

        annonce_id = str(ad.get("annonceId") or ad.get("id") or "")
        reference = ad.get("reference", "") or ""

        prix = ad.get("prixAffiche")
        surface = ad.get("surface")
        terrain = ad.get("surfaceTerrain")
        pieces = ad.get("nbPieces")
        description = (ad.get("descriptionFr") or "")[:1200]

        ville = ad.get("communeNom") or ad.get("localiteNom") or ""
        code_postal = ad.get("codePostal", "") or ""
        departement = ad.get("inseeDepartement", "") or (code_postal[:2] if code_postal else "")

        type_bien = _TYPE_LABEL.get(type_code, "maison")

        photos = []
        photo_url = ad.get("urlPhotoPrincipale", "")
        if photo_url:
            photos.append(photo_url)

        # DPE : seule la date de réalisation est dans le résumé (lettre absente).
        dpe = None

        bits = []
        if pieces:
            bits.append(f"{int(pieces)} p.")
        if surface:
            bits.append(f"{surface}m²")
        titre = f"{type_bien.title()} {' '.join(bits)} {ville} ({code_postal})".strip()

        return {
            "source": "notaires_notacoeur_18",
            "url": url,
            "id_annonce": annonce_id or reference or url,
            "titre": titre[:150],
            "type_bien": type_bien,
            "description": description,
            "departement": departement,
            "ville": ville[:80],
            "code_postal": code_postal,
            "surface": float(surface) if surface else None,
            "surface_terrain": float(terrain) if terrain else None,
            "pieces": int(pieces) if pieces else None,
            "chambres": None,
            "prix": float(prix) if prix else None,
            "photos": photos,
            "dpe": dpe,
            "agence": f"NOTACOEUR (Bourges) - ref {reference}".strip(),
        }
    except Exception:
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
    print(f"\nTotal Notacoeur Bourges: {len(biens)} annonces")
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

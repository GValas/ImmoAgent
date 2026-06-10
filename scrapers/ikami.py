"""scrapers/ikami.py — Réseau Ikami (ex i-Particuliers), mandataires

Méthode : api_inoff (httpx) — API JSON interne, PAS de Playwright.
  Les pages /ventes et /recherche/vente injectent les annonces en JS
  (fetch côté navigateur) → le HTML brut est vide d'annonces. Mais le JS
  (assets/js/search_map_interface.js) appelle un endpoint JSON lisible :

  GET /data_management/api_search_ads.php?type_offre=vente&departement={NN}&page={N}
      &sort=newest
  → {"ads":[...], "page", "totalPages", "totalResults", ...}

Filtre département : CÔTÉ SERVEUR via le param `departement={NN}` (vérifié,
  aucune fuite — chaque dept ne renvoie que ses propres annonces). Post-filtre
  strict `cp[:2] == dept` conservé par sécurité.

Champs JSON utiles : id, titre, corps (description), prix / prix_display,
  type_bien (Maison/Appartement/Immeuble/Terrain...), surface_habitable,
  surface_terrain, nb_pieces, chambres, ville / ville_google, cp, departement,
  class_energie (DPE), nb_image, agence/conseiller_*.

Détail : /annonce/{id}/{type_offre}/{type_bien}/{ville}/{prix}
Photos  : https://ikami.fr/assets/images/annonces/small/{id}_{n}.jpg
          (pour la France ; suffixe pays sinon — hors zone ici).

Type de bien : on ne garde que maisons / propriétés (exclut appartement,
  terrain, immeuble, local, parking...).

Couverture : réseau national (FR/CH/ES), ~3500 ventes. Sur les départements
  cibles l'inventaire est variable (89/41 ~40, 45/36 ~8, 72/53 = 0).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx

BASE_URL = "https://ikami.fr"
API_URL = f"{BASE_URL}/data_management/api_search_ads.php"
MAX_PAGES = 12
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE_URL}/recherche/vente",
}

# Départements cibles → l'API accepte directement le code (param `departement`)
DEPT_CIBLES = {"72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"}

# Types de bien (champ type_bien) à conserver : maisons / propriétés / fermes...
_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|mas|g[iî]te|corps de ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"loft|studio|chambre",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            if dept not in DEPT_CIBLES:
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[Ikami] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Ikami] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        params = {
            "type_offre": "vente",
            "departement": dept,
            "page": str(page),
            "sort": "newest",
        }
        r = await client.get(API_URL, params=params)
        if r.status_code != 200:
            break
        try:
            data = r.json()
        except Exception:
            break

        ads = data.get("ads") or []
        if not ads:
            break

        for ad in ads:
            try:
                bien = _parse_ad(ad, dept)
            except Exception:
                continue
            if not bien:
                continue

            # Sécurité : on n'accepte que le département cible (filtre serveur déjà OK)
            if bien["code_postal"] and bien["code_postal"][:2] != dept:
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
            biens.append(bien)

        total_pages = _to_int(data.get("totalPages"))
        if total_pages and page >= total_pages:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_ad(ad: dict, dept: str) -> dict | None:
    type_bien_raw = (ad.get("type_bien") or "").strip()
    if _EXCLUDE_TYPE.search(type_bien_raw) and not _KEEP_TYPE.search(type_bien_raw):
        return None
    if not _KEEP_TYPE.search(type_bien_raw):
        # type inconnu/ambigu → on exclut par prudence
        return None
    type_bien = type_bien_raw.lower() or "maison"

    ad_id = ad.get("id")
    if ad_id is None:
        return None
    id_annonce = str(ad_id)

    ville = (ad.get("ville_google") or ad.get("ville") or "").strip()
    code_postal = str(ad.get("cp") or "").strip()

    # URL détail : /annonce/{id}/{type_offre}/{type_bien}/{ville}/{prix}
    type_offre = (ad.get("type_offre") or "Vente").strip()
    prix = _to_float(ad.get("prix_display") or ad.get("prix"))
    prix_seg = str(int(prix)) if prix else "0"
    ville_seg = _slug(ville) or "lieu"
    url = (
        f"{BASE_URL}/annonce/{id_annonce}/{_slug(type_offre) or 'Vente'}/"
        f"{_slug(type_bien_raw) or 'Bien'}/{ville_seg}/{prix_seg}"
    )

    titre = (ad.get("titre") or "").strip()
    if not titre:
        titre = f"{type_bien_raw} {ville}".strip()

    description = (ad.get("corps") or "").strip()

    surface = _to_float(ad.get("surface_habitable")) or _to_float(
        ad.get("surface_carrez")
    )
    surface_terrain = _to_float(ad.get("surface_terrain"))
    pieces = _to_int(ad.get("nb_pieces"))
    chambres = _to_int(ad.get("chambres"))

    dpe = (ad.get("class_energie") or "").strip().upper() or None
    if dpe and dpe not in {"A", "B", "C", "D", "E", "F", "G"}:
        dpe = None

    # Photos : {id}_1.jpg .. {id}_N.jpg (small) — version FR (pas de suffixe pays)
    nb_image = _to_int(ad.get("nb_image")) or 0
    nb = min(nb_image, PHOTOS_PER_CARD)
    photos = [
        f"{BASE_URL}/assets/images/annonces/small/{id_annonce}_{i}.jpg"
        for i in range(1, nb + 1)
    ]

    agence = (ad.get("agence") or "").strip()
    if not agence:
        prenom = (ad.get("conseiller_prenom") or "").strip()
        nom = (ad.get("conseiller_nom") or "").strip()
        agence = f"{prenom} {nom}".strip()
    agence = f"Ikami — {agence}".strip(" —") if agence else "Ikami"

    return {
        "source": "ikami",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": agence,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _to_float(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v else None
    s = re.sub(r"[^\d.]", "", str(v).replace(",", "."))
    try:
        f = float(s)
        return f if f else None
    except ValueError:
        return None


def _to_int(v) -> int | None:
    f = _to_float(v)
    return int(f) if f is not None else None


def _slug(text: str) -> str:
    if not text:
        return ""
    s = text.strip().lower()
    s = re.sub(r"[àáâãä]", "a", s)
    s = re.sub(r"[èéêë]", "e", s)
    s = re.sub(r"[ìíîï]", "i", s)
    s = re.sub(r"[òóôõö]", "o", s)
    s = re.sub(r"[ùúûü]", "u", s)
    s = re.sub(r"[ç]", "c", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


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
    print(f"\nTotal Ikami: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
            f" — DPE {b.get('dpe') or '?'} — {len(b['photos'])} photos"
        )

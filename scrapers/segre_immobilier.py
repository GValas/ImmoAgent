"""scrapers/segre_immobilier.py — Segré Immobilier (Segré-en-Anjou, Maine-et-Loire 49)

Méthode : scrape_simple (httpx) — API JSON (Wappler/DMX AppConnect).
Agence indépendante du Segréen (depuis 2006 : Segré-en-Anjou Bleu, Ombrée
d'Anjou, Erdre-en-Anjou…). Biens majoritairement en 49, un peu de 53 (Mayenne)
et 44 (Loire-Atlantique). Le 44 est HORS périmètre → POST-FILTRE strict.

Le portail est une SPA AngularJS ({{ }}) ; les données viennent d'un endpoint
JSON unique (pas de Playwright nécessaire) :
  GET /dmxConnect/api/listeaffaires.php?codeagence=lens49&typetransaction=VENTE
  → {"selectionaffaires":[ {Code, BienCP, BienVille, PrixMandatEuro, TypeAffaire,
       SurfHab, SurfTerrain, NbPieces, NbChambres, TextePub, DPELettre, NumPhoto,
       CodeAgence, ...}, ... ]}
Photos : https://www.selection-immo.com/Photos/{CodeAgence}/Photos/{Code}[-N].jpg
Détail : /ficheaffaire.php?code={Code}

Interface : async def search(criteres: dict) -> list[dict]
"""
from __future__ import annotations

import re

from scrapers._base import get_with_retry, make_client, standalone_main

BASE_URL = "https://segreimmobilier.fr"
API_URL = f"{BASE_URL}/dmxConnect/api/listeaffaires.php"
PHOTO_BASE = "https://www.selection-immo.com/Photos"
CODE_AGENCE = "lens49"
SOURCE = "segre_immobilier"
LABEL = "SegreImmo"
PHOTOS_MAX = 10

_EXCLUDE_TYPE = re.compile(
    r"terrain|local|b[aâ]timent|immeuble|fonds|commerc|parking|garage|professionnel",
    re.IGNORECASE,
)


def _to_float(v) -> float | None:
    if v is None:
        return None
    s = re.sub(r"[^\d.]", "", str(v).replace(",", "."))
    if not s:
        return None
    try:
        f = float(s)
        return f if f > 0 else None
    except ValueError:
        return None


def _to_int(v) -> int | None:
    f = _to_float(v)
    return int(f) if f else None


def _map_type(t: str) -> str | None:
    """Normalise TypeAffaire → maison/appartement ; None si hors habitat."""
    if not t:
        return None
    if _EXCLUDE_TYPE.search(t):
        return None
    low = t.lower()
    if "appartement" in low or "studio" in low:
        return "appartement"
    # Maison / Propriété / Fermette / Maison ancienne / Longère…
    return "maison"


def _build_photos(code: str, code_agence: str, num_photo) -> list[str]:
    n = _to_int(num_photo) or 0
    base = f"{PHOTO_BASE}/{code_agence}/Photos/{code}"
    photos = [f"{base}.jpg"]
    for i in range(1, min(n, PHOTOS_MAX)):
        photos.append(f"{base}-{i}.jpg")
    return photos[:PHOTOS_MAX]


def _parse_affaire(a: dict) -> dict | None:
    code = str(a.get("Code") or "").strip()
    if not code:
        return None

    type_bien = _map_type(a.get("TypeAffaire") or "")
    if type_bien is None:
        return None

    cp = (a.get("BienCP") or "").strip()
    ville = (a.get("BienVille") or "").strip().title()
    prix = _to_float(a.get("PrixMandatEuro")) or _to_float(a.get("PrixNetVendeurEuro"))

    dpe = (a.get("DPELettre") or "").strip().upper() or None
    if dpe and dpe not in {"A", "B", "C", "D", "E", "F", "G"}:
        dpe = None

    desc = re.sub(r"\s+", " ", (a.get("TextePub") or "")).strip()
    titre = f"{(a.get('TypeAffaire') or type_bien).strip()} {ville}".strip()
    code_agence = (a.get("CodeAgence") or CODE_AGENCE).strip() or CODE_AGENCE

    return {
        "source": SOURCE,
        "url": f"{BASE_URL}/ficheaffaire.php?code={code}",
        "id_annonce": code,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": desc[:1200],
        "departement": cp[:2] if cp else None,
        "ville": ville[:80],
        "code_postal": cp or None,
        "surface": _to_float(a.get("SurfHab")),
        "surface_terrain": _to_float(a.get("SurfTerrain")),
        "pieces": _to_int(a.get("NbPieces")),
        "chambres": _to_int(a.get("NbChambres")),
        "prix": prix,
        "photos": _build_photos(code, code_agence, a.get("NumPhoto")),
        "dpe": dpe,
        "agence": "Segré Immobilier",
    }


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen: set[str] = set()

    async with make_client() as client:
        r = await get_with_retry(
            client, API_URL,
            params={"codeagence": CODE_AGENCE, "typetransaction": "VENTE"},
        )
        if r is None or r.status_code != 200:
            print(f"[{LABEL}] API injoignable (status={getattr(r, 'status_code', None)})")
            return results
        try:
            affaires = r.json().get("selectionaffaires", [])
        except Exception as e:
            print(f"[{LABEL}] JSON invalide : {e}")
            return results

        for a in affaires:
            try:
                bien = _parse_affaire(a)
            except Exception:
                continue
            if not bien:
                continue
            cp = str(bien.get("code_postal") or "")
            if not cp or cp[:2] not in departements:
                continue  # POST-FILTRE dept STRICT (0 fuite : écarte le 44…)
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

    print(f"[{LABEL}] Total : {len(results)} annonces")
    return results


if __name__ == "__main__":
    standalone_main(search, "Segré Immobilier")

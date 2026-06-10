"""scrapers/lelievre_immobilier.py — Lelièvre Immobilier (réseau ~15 agences Grand Ouest + Paris)

Méthode : api_inoff (httpx) — JSON statique.
La page /recherche est rendue côté client (template underscore + carte Google
Maps) : les annonces NE SONT PAS dans le HTML SSR. Mais le widget de recherche
charge un fichier JSON statique listant TOUTES les annonces de vente :
    /sites/default/files/annonces/json/venteAnnonces.json
On le récupère en une requête et on filtre/parse localement.

Filtre département : aucun param serveur — on télécharge le national (~367 biens)
puis on POST-FILTRE strictement sur field_annonce_departement / code_postal[:2].
Le réseau couvre largement le Grand Ouest + Paris (35, 75, 44, 56, 92, 93…), donc
le post-filtre est indispensable pour rester sur les 11 départements cibles.

Champs JSON utiles :
  - nid / field_annonce_reference  → identifiants
  - url                            → /vente/maison-{ville}-ref-{id}
  - title                          → "Vente - Maison 8 pièces 222 m²"
  - field_annonce_ville / _code_postal / _departement
  - field_annonce_prix_brute       → prix numérique (field_annonce_prix = "572 000")
  - field_annonce_surface / _surface_terrain (numériques)
  - field_annonce_pieces / _chambres
  - field_annonce_type_bien        → code (14 = maison, 82337 = maison/villa)
  - first_image_url + field_annonce_diaporama (galerie d'<img>)
  - field_accroche                 → description courte (souvent None)

Type de bien : déduit du segment d'URL (/vente/{type}-...) — on ne garde que
maisons / propriétés / villas / fermes / longères, etc.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx

BASE_URL = "https://www.lelievre-immobilier.com"
JSON_URL = f"{BASE_URL}/sites/default/files/annonces/json/venteAnnonces.json"
PHOTOS_PER_CARD = 12

# Départements cibles (post-filtre strict)
TARGET_DEPTS = {"72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Referer": f"{BASE_URL}/recherche?transaction=vente&type_bien-1=Maison",
}

# Codes type_bien correspondant à des maisons (sécurité en complément de l'URL)
_KEEP_TYPE_CODES = {"14", "82337"}

# Types de bien (segment d'URL) à conserver : maisons / propriétés / fermes...
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme",
    re.IGNORECASE,
)
# Types explicitement exclus (segment d'URL)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|stationnement|immeuble|"
    r"bureau|entrepot|entrepôt|atelier|hotel|hôtel|boutique|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    departements &= TARGET_DEPTS  # double sécurité : ne jamais sortir de la zone
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()
    per_dept: dict[str, int] = {}

    try:
        async with httpx.AsyncClient(
            headers=HEADERS, follow_redirects=True, timeout=40
        ) as client:
            r = await client.get(JSON_URL)
            if r.status_code != 200:
                print(f"[Lelievre] JSON HTTP {r.status_code} — abandon")
                return results
            try:
                data = r.json()
            except Exception as e:
                print(f"[Lelievre] JSON illisible: {e}")
                return results
    except Exception as e:
        print(f"[Lelievre] Erreur réseau: {e}")
        return results

    if not isinstance(data, list):
        print("[Lelievre] Format JSON inattendu")
        return results

    for item in data:
        try:
            bien = _parse_item(item)
        except Exception:
            continue
        if not bien:
            continue

        dept = bien["departement"]
        # Post-filtre STRICT : département cible + cohérence CP[:2]
        if dept not in departements:
            continue
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
        results.append(bien)
        per_dept[dept] = per_dept.get(dept, 0) + 1

    for d in sorted(per_dept):
        print(f"[Lelievre] Dept {d}: {per_dept[d]} annonces")
    print(f"[Lelievre] Total retenu: {len(results)}")
    await asyncio.sleep(0)  # garder la signature async cohérente
    return results


def _parse_item(item: dict) -> dict | None:
    href = (item.get("url") or "").strip()
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL : /vente/{type}-{ville}-ref-{id}
    parts = [p for p in href.split("/") if p]
    type_seg = parts[1] if len(parts) > 1 else ""
    type_code = str(item.get("field_annonce_type_bien") or "")

    keep = bool(_KEEP_TYPE.search(type_seg)) or type_code in _KEEP_TYPE_CODES
    excluded = bool(_EXCLUDE_TYPE.search(type_seg)) and not _KEEP_TYPE.search(type_seg)
    if excluded or not keep:
        return None

    # Localisation
    dept = str(item.get("field_annonce_departement") or "").zfill(2)
    cp = str(item.get("field_annonce_code_postal") or "").strip()
    if cp and not dept:
        dept = cp[:2]
    ville = _clean(item.get("field_annonce_ville"))
    if ville:
        ville = ville.title()

    # Identifiants
    nid = str(item.get("nid") or "").strip()
    ref = _clean(item.get("field_annonce_reference"))
    id_annonce = nid or ref or url

    # Titre
    titre = _clean_html(item.get("title")) or f"Maison {ville}".strip()

    # Prix : field_annonce_prix_brute est numérique
    prix = _to_float(item.get("field_annonce_prix_brute"))
    if prix is None:
        prix = _parse_price(item.get("field_annonce_prix"))
    if not prix:  # 0 ou None → prix masqué / programme neuf : on ignore le filtre prix
        prix = None

    surface = _to_float(item.get("field_annonce_surface"))
    if surface == 0:
        surface = None
    surface_terrain = _to_float(item.get("field_annonce_surface_terrain"))
    if surface_terrain == 0:
        surface_terrain = None

    pieces = _to_int(item.get("field_annonce_pieces"))
    chambres = _to_int(item.get("field_annonce_chambres"))
    if chambres == 0:
        chambres = None

    etage = _to_int(item.get("field_annonce_etage"))

    description = _clean(item.get("field_accroche")) or ""

    # Type lisible
    type_bien = re.sub(r"^\d+-", "", type_seg).split("-")[0] or "maison"

    # Photos : first_image_url + galerie diaporama
    photos: list[str] = []
    fimg = item.get("first_image_url")
    if fimg:
        photos.append(_abs_img(fimg))
    for m in re.finditer(r'<img[^>]+src="([^"]+)"', item.get("field_annonce_diaporama") or ""):
        src = _abs_img(m.group(1))
        if src not in photos:
            photos.append(src)
    photos = [p for p in photos if p and not p.startswith("data:")][:PHOTOS_PER_CARD]

    return {
        "source": "lelievre_immobilier",
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
        "dpe": None,
        "agence": "Lelièvre Immobilier",
        "etage": etage if etage else None,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clean(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _clean_html(v) -> str:
    """Retire les balises (ex: 'Maison 8 pièces 222 m<sup>2</sup>')."""
    s = _clean(v)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _abs_img(src: str) -> str:
    src = src.strip()
    if not src:
        return ""
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return BASE_URL + src
    return src


def _to_float(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    cleaned = re.sub(r"[^\d.]", "", str(v).replace("\xa0", "").replace(" ", ""))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _to_int(v) -> int | None:
    f = _to_float(v)
    return int(f) if f is not None else None


def _parse_price(text) -> float | None:
    cleaned = re.sub(r"[^\d]", "", str(text or ""))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
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
    print(f"\nTotal Lelièvre Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['pieces'] or '?'}p — {b['type_bien']} — {b['ville']}"
        )

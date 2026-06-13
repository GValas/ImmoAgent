"""
scrapers/cimm.py — CIMM Immobilier (réseau ~220 agences, cimm.com)
Méthode : api_inoff (httpx) — API REST publique https://api.cimm.com/api/realties
Interface : async def search(criteres: dict) -> list[dict]

Notes parsing :
- L'API renvoie l'inventaire NATIONAL ; le filtre localisation côté serveur
  (city_cp, department) est CASSÉ (ignoré). On post-filtre par city_cp[:2].
- operation=vente (achat) + sold_rented=false côté serveur (ces deux-là marchent).
- product_family / realty_family côté serveur n'est PAS appliqué → on filtre
  realty_family ('maison' / 'villa') côté client.
- Inventaire vente actif ~2000 biens → ~4 pages de 500. Plafond MAX_PAGES.
- DPE extrait du texte (fr_text) par regex.
- Fiche : https://www.cimm.com/bien/{id}
"""
import asyncio
import re

import httpx

API_URL = "https://api.cimm.com/api/realties"
SITE_BASE = "https://www.cimm.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Origin": SITE_BASE,
    "Referer": f"{SITE_BASE}/",
}

PAGE_SIZE = 500
MAX_PAGES = 8  # ~4000 biens max, large marge sur l'inventaire vente (~2000)

# Familles de biens considérées comme "maison" (l'API renvoie aussi
# appartement / immeuble / terrain / local que l'on écarte).
MAISON_FAMILIES = {"maison", "villa", "propriete", "propriété", "chateau", "château"}

# Champs demandés (réduit la charge utile)
FIELDS = (
    "id,slug,realty_family,operation,fr_title,fr_text,price,"
    "room_number,bedroom_number,inhabitable_surface,field_surface,"
    "city_name,city_cp,real_cp,real_city,photo,realtyphoto_set,reference"
)

DPE_RE = re.compile(r"\bDPE\s*[:\-]?\s*([A-G])\b", re.IGNORECASE)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max")
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min")

    all_items = await _fetch_all()

    results = []
    for item in all_items:
        # Ne garder que les maisons / propriétés
        fam = (item.get("realty_family") or "").lower()
        if fam not in MAISON_FAMILIES:
            continue

        cp = str(item.get("city_cp") or item.get("real_cp") or "")
        dept = cp[:2] if len(cp) >= 2 else ""
        if departements and dept not in departements:
            continue

        bien = _parse_item(item, dept)
        if not bien:
            continue

        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        results.append(bien)

    # Déduplication par id_annonce
    seen = set()
    deduped = []
    for b in results:
        if b["id_annonce"] in seen:
            continue
        seen.add(b["id_annonce"])
        deduped.append(b)

    # Log par département
    by_dept = {}
    for b in deduped:
        by_dept.setdefault(b["departement"], []).append(b)
    for dept, items in sorted(by_dept.items()):
        print(f"[CIMM] Dept {dept}: {len(items)} annonces")

    return deduped


async def _fetch_all() -> list[dict]:
    all_items = []
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=30) as client:
        offset = 0
        for _ in range(MAX_PAGES):
            params = {
                "operation": "vente",
                "sold_rented": "false",
                "ordering": "-modification_date",
                "fields": FIELDS,
                "limit": PAGE_SIZE,
                "offset": offset,
            }
            try:
                r = await client.get(API_URL, params=params)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                print(f"[CIMM] Erreur fetch offset={offset}: {e}")
                break

            items = data.get("results", [])
            if not items:
                break
            all_items.extend(items)

            count = data.get("count", 0)
            offset += PAGE_SIZE
            if offset >= count or not data.get("next"):
                break
            await asyncio.sleep(0.3)

    return all_items


def _parse_item(item: dict, dept: str) -> dict | None:
    try:
        ad_id = item.get("id")
        if not ad_id:
            return None
        ad_id = str(ad_id)

        prix = item.get("price")
        prix = float(prix) if prix else None

        surface = item.get("inhabitable_surface")
        surface = float(surface) if surface else None

        terrain = item.get("field_surface")
        terrain = float(terrain) if terrain else None

        pieces = item.get("room_number")
        chambres = item.get("bedroom_number")

        ville = item.get("city_name") or item.get("real_city") or ""
        cp = str(item.get("city_cp") or item.get("real_cp") or "")

        titre = (item.get("fr_title") or f"Maison — {ville} ({cp})").strip()
        description = (item.get("fr_text") or "").strip()

        # DPE depuis le texte
        dpe = None
        m = DPE_RE.search(description)
        if m:
            dpe = m.group(1).upper()

        # Photos
        photos = []
        for ph in item.get("realtyphoto_set") or []:
            img = ph.get("image") if isinstance(ph, dict) else None
            if img and isinstance(img, str) and img.startswith("http"):
                photos.append(img)
        if not photos:
            main = item.get("photo")
            if isinstance(main, str) and main.startswith("http"):
                photos.append(main)
        photos = photos[:10]

        return {
            "source": "cimm",
            "url": f"{SITE_BASE}/bien/{ad_id}",
            "id_annonce": ad_id,
            "titre": titre[:150],
            "type_bien": "maison",
            "description": description[:1200],
            "departement": dept,
            "ville": str(ville)[:80],
            "code_postal": cp,
            "surface": surface,
            "surface_terrain": terrain,
            "pieces": int(pieces) if pieces else None,
            "chambres": int(chambres) if chambres else None,
            "prix": prix,
            "photos": photos,
            "dpe": dpe,
            "agence": "Cimm Immobilier",
        }
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    biens = asyncio.run(search({
        "departements": criteres.departements,
        "prix_max": criteres.prix_max,
        "prix_min": getattr(criteres, "prix_min", 0),
        "surface_min": criteres.surface_min,
    }))
    print(f"\nTotal CIMM: {len(biens)} annonces")
    targets = {str(d).zfill(2) for d in criteres.departements}
    hors = [b for b in biens if b["code_postal"][:2] not in targets]
    print(f"Hors-département (devrait être 0): {len(hors)}")
    for b in biens[:8]:
        print(f"  [{b['code_postal']}] {b['titre'][:55]} — {b['prix']}€ — "
              f"{b['surface']}m² — {b['ville']}")

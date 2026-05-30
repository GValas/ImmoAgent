"""
scrapers/liberkeys.py — Liberkeys (agence immobilière en ligne)

Méthode : api_inoff (httpx, pas de Playwright)
Backend : https://api.liberkeys.com/portal/properties  (alimente le SPA Vue portail.liberkeys.com)

Particularités découvertes en sondant l'API :
  - L'endpoint liste accepte ?region={id}&max_results=&offset=&order=recent_desc
  - La pagination par `offset` est CASSÉE côté serveur (renvoie vide au-delà du 1er lot)
    et `max_results` est plafonné à 500. On ne peut donc PAS paginer l'inventaire national.
  - Beaucoup de codes `region` sont IGNORÉS (renvoient l'inventaire national entier, 865 biens).
    Seuls certains codes `region` filtrent réellement par région administrative.
  - Les départements cibles sont couverts par 2 régions qui filtrent correctement :
        region 13  = Centre-Val de Loire  (28, 37, 41, 49 partiel...)
        region 17  = Pays de la Loire     (44, 49, 72, 85)
    On interroge ces régions puis on POST-FILTRE par code_postal[:2] ∈ départements cibles
    (les seuls dept cibles réellement présents chez Liberkeys sont 28/37/41/49/72 ;
     45/36/18/89/58/53 n'ont aucun bien — agence centrée sur les grandes villes).
  - L'adresse renvoyée se termine toujours par "{cp 5 chiffres} {ville}".
  - Détail enrichi : /portal/properties/{slug}  (description, dpe_label, ges_label, photos HD).

Fiche publique : https://portail.liberkeys.com/{slug}
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

import httpx

API_BASE = "https://api.liberkeys.com/portal/properties"
PORTAL_BASE = "https://portail.liberkeys.com"

# Régions administratives Liberkeys qui filtrent réellement et contiennent des dept cibles
TARGET_REGIONS = [13, 17]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Origin": PORTAL_BASE,
    "Referer": f"{PORTAL_BASE}/",
}

# Types considérés comme maison / propriété (on exclut appartement, terrain, local, parking)
_HOUSE_TYPES = {
    "maison", "villa", "propriété", "propriete", "longère", "longere",
    "ferme", "manoir", "château", "chateau", "moulin", "demeure", "mas",
}

_ZIP_RE = re.compile(r"\b(\d{5})\b")


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    async with httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True) as client:
        # 1) Récupère les listes des régions cibles, dédup par slug
        listings: dict[str, dict] = {}
        for region in TARGET_REGIONS:
            try:
                items = await _fetch_region(client, region)
            except Exception as e:
                print(f"[Liberkeys] Erreur region {region}: {e}")
                continue
            for it in items:
                slug = it.get("slug")
                if slug and slug not in listings:
                    listings[slug] = it

        # 2) Post-filtre département + prix/surface (filtre dept côté serveur non fiable)
        retained = []
        for it in listings.values():
            if it.get("is_sold") or it.get("is_unavailable"):
                continue
            type_bien = (it.get("type") or "").strip().lower()
            if type_bien not in _HOUSE_TYPES:
                continue

            cp = _extract_cp(it.get("address", ""))
            dept = cp[:2] if cp else ""
            if departements and dept not in departements:
                continue

            prix = it.get("price")
            if prix_max and prix and prix > prix_max:
                continue
            if prix_min and prix and prix < prix_min:
                continue
            surface = it.get("surface")
            if surface_min and surface and surface < surface_min:
                continue

            retained.append((it, dept, cp))

        # 3) Enrichit chaque bien retenu via l'endpoint détail (description, dpe, photos HD)
        sem = asyncio.Semaphore(6)

        async def build(entry):
            it, dept, cp = entry
            detail = await _fetch_detail(client, sem, it.get("slug"))
            return _to_bien(it, detail, dept, cp)

        results = await asyncio.gather(*(build(e) for e in retained))
        results = [b for b in results if b]

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[Liberkeys] Dept {dept}: {n} annonces")

    return results


async def _fetch_region(client: httpx.AsyncClient, region: int) -> list[dict]:
    params = {
        "region": region,
        "max_results": 500,
        "offset": 0,
        "include_sold_properties": "false",
        "order": "recent_desc",
    }
    r = await client.get(API_BASE, params=params)
    r.raise_for_status()
    return r.json().get("properties", [])


async def _fetch_detail(client: httpx.AsyncClient, sem: asyncio.Semaphore, slug: str) -> dict:
    if not slug:
        return {}
    async with sem:
        try:
            r = await client.get(f"{API_BASE}/{slug}")
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
    return {}


def _extract_cp(address: str) -> str:
    m = _ZIP_RE.search(address or "")
    return m.group(1) if m else ""


def _extract_ville(address: str, cp: str) -> str:
    """L'adresse se termine par '{cp} {ville}'."""
    if cp and cp in address:
        tail = address.split(cp, 1)[1].strip(" ,")
        if tail:
            return tail[:80]
    return ""


_DPE_LETTERS = {"A", "B", "C", "D", "E", "F", "G"}


def _to_bien(it: dict, detail: dict, dept: str, cp: str) -> dict | None:
    try:
        slug = it.get("slug")
        if not slug:
            return None

        address = it.get("address", "") or ""
        ville = _extract_ville(address, cp)

        type_raw = (it.get("type") or "").strip()
        type_bien = "maison" if type_raw.lower() in _HOUSE_TYPES else (type_raw.lower() or "maison")

        # Photos : préférer HD du détail, sinon les low_quality de la liste
        photos = detail.get("high_quality_media") or it.get("low_quality_media") or []
        photos = [p for p in photos if isinstance(p, str) and p.startswith("http")][:12]

        # DPE : dpe_label = lettre (A..G) si dispo
        dpe = None
        lbl = (detail.get("dpe_label") or "").strip().upper()
        if lbl in _DPE_LETTERS:
            dpe = lbl

        titre = (detail.get("seo_title") or "").strip()
        if not titre:
            titre = f"{type_raw or 'Maison'} {it.get('room_count') or ''} pièces {ville}".strip()

        prix = it.get("price")
        surface = it.get("surface")
        pieces = it.get("room_count")
        chambres = it.get("bedroom_count")

        return {
            "source": "liberkeys",
            "url": f"{PORTAL_BASE}/{slug}",
            "id_annonce": str(detail.get("reference") or slug),
            "titre": titre[:150],
            "type_bien": type_bien,
            "description": (detail.get("description") or "").strip()[:1500] or None,
            "departement": dept,
            "ville": ville or None,
            "code_postal": cp or None,
            "surface": float(surface) if surface else None,
            "surface_terrain": None,
            "pieces": int(pieces) if pieces else None,
            "chambres": int(chambres) if chambres else None,
            "prix": float(prix) if prix else None,
            "dpe": dpe,
            "photos": photos,
            "agence": "Liberkeys",
        }
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    biens = asyncio.run(
        search({
            "departements": criteres.departements,
            "prix_max": criteres.prix_max,
            "prix_min": getattr(criteres, "prix_min", 0),
            "surface_min": criteres.surface_min,
        })
    )
    print(f"\nTotal Liberkeys: {len(biens)} annonces")
    depts_vus = sorted({b["departement"] for b in biens})
    print(f"Départements vus: {depts_vus}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€ — {b.get('surface')}m²"
            f" — {b['code_postal']} {b['ville']} — DPE {b['dpe']}"
        )

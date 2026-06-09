"""scrapers/maisonblanche.py — Maison Blanche Immobilier (réseau d'agences national)

Méthode : scrape_simple (httpx) — sitemap.xml + pages détail SSR (JSON-LD)

Particularité : plateforme Netty, liste de recherche rendue côté client (CSR React,
inexploitable en httpx). En revanche le **sitemap.xml** expose toutes les annonces
détail, et **chaque page détail est SSR** : elle contient un bloc
<script type="application/ld+json"> @type=Product avec prix, code postal, ville,
surface, pièces, photos et type. Le prix (affiché en clair "229 000 €") est donc
disponible sans Playwright.

Stratégie filtre département (AUCUN filtre serveur — réseau national, majorité hors
zone) :
  1. Récupérer sitemap.xml → ne garder que les URL détail /vente/{slug}-{cp},{REF}
     dont le **code postal suffixe** a un préfixe dans `departements` (pré-filtre).
  2. Charger chaque page détail retenue, parser le JSON-LD, et RE-VÉRIFIER
     code_postal[:2] == dept (post-filtre strict → 0 fuite).

URL détail : https://www.maisonblanche.immo/vente/{type}-{...}-{ville}-{cp},{REF}
  ex : /vente/maison-ancienne-8-pieces-le-plessis-grammoire-49124,VM4706

Champs JSON-LD (offers / itemOffered) :
  - price                 → prix
  - itemOffered.@type     → House / Apartment / LandLot ...
  - address.postalCode    → code_postal ; address.addressLocality → ville
  - floorSize.value       → surface (m²)
  - numberOfRooms         → pièces
  - photo[].url           → photos
Terrain extrait du HTML (label "Terrain : NN NNN m²"). DPE non exposé proprement → None.

Couverture zone cible : surtout le 49 (~42 biens, agence Angers). Volume marginal
sur 37/28/45/53. National sinon (33/17/79/85 majoritaires) → filtre dept critique.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx

BASE_URL = "https://www.maisonblanche.immo"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
PHOTOS_PER_CARD = 10
MAX_DETAILS_PER_DEPT = 80   # garde-fou
CONCURRENCY = 6

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# URL détail : .../vente/{slug}...-{cp},{REF}
_DETAIL_RE = re.compile(r"/vente/[^/]+-(\d{5}),([A-Za-z0-9]+)$")

# On ne garde que maisons / propriétés (le segment de type est en tête du slug)
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|stationnement|fonds|immeuble|immobilier-pro|"
    r"droit-au-bail|local|commerce|garage|parking|bureau",
    re.IGNORECASE,
)

# JSON-LD @type Product → catégorie de bien
_JSONLD_HOUSE = {"house", "singlefamilyresidence"}


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1. Sitemap → URL détail vente
        try:
            r = await client.get(SITEMAP_URL)
            r.raise_for_status()
        except Exception as e:
            print(f"[MaisonBlanche] Erreur sitemap : {e}")
            return []

        locs = re.findall(r"<loc>(.*?)</loc>", r.text)
        # Pré-filtre : URL détail + cp dans la zone + type gardé
        by_dept: dict[str, list[tuple[str, str]]] = {d: [] for d in departements}
        for loc in locs:
            m = _DETAIL_RE.search(loc)
            if not m:
                continue
            cp, ref = m.group(1), m.group(2)
            dept = cp[:2]
            if dept not in by_dept:
                continue
            type_seg = loc.split("/vente/")[1]
            if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
                continue
            if not _KEEP_TYPE.search(type_seg):
                continue
            by_dept[dept].append((loc, ref))

        # 2. Charger les pages détail retenues, par département
        sem = asyncio.Semaphore(CONCURRENCY)
        for dept in departements:
            urls = by_dept.get(dept, [])[:MAX_DETAILS_PER_DEPT]
            if not urls:
                print(f"[MaisonBlanche] Dept {dept}: 0 annonce dans le sitemap")
                continue

            async def _fetch(loc: str, ref: str, d: str = dept):
                async with sem:
                    return await _scrape_detail(client, loc, ref, d)

            tasks = [_fetch(loc, ref) for loc, ref in urls]
            biens = await asyncio.gather(*tasks)
            biens = [b for b in biens if b]

            kept = []
            for bien in biens:
                # Post-filtre dept STRICT (0 fuite)
                if not bien["code_postal"] or bien["code_postal"][:2] != dept:
                    continue
                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue
                kept.append(bien)

            results.extend(kept)
            print(f"[MaisonBlanche] Dept {dept}: {len(kept)} annonces "
                  f"({len(urls)} candidats sitemap)")
            await asyncio.sleep(0.5)

    return results


async def _scrape_detail(
    client: httpx.AsyncClient, url: str, ref: str, dept: str
) -> dict | None:
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return None
    except Exception:
        return None

    html = r.text
    data = _extract_jsonld(html)
    if not data:
        return None

    offers = data.get("offers", {}) or {}
    item = offers.get("itemOffered", {}) or {}
    addr = item.get("address", {}) or {}

    code_postal = str(addr.get("postalCode") or "").strip()
    # secours : cp depuis l'URL
    if not code_postal:
        m = _DETAIL_RE.search(url)
        if m:
            code_postal = m.group(1)
    ville = str(addr.get("addressLocality") or "").strip()

    prix = _to_float(offers.get("price"))

    surface = None
    fs = item.get("floorSize") or {}
    if isinstance(fs, dict):
        surface = _to_float(fs.get("value"))

    pieces = _to_int(item.get("numberOfRooms"))

    # Type de bien
    jtype = str(item.get("@type") or "").lower()
    type_seg = url.split("/vente/")[1].split("-")[0]
    if jtype in _JSONLD_HOUSE:
        type_bien = "maison"
    elif type_seg:
        type_bien = type_seg
    else:
        type_bien = "maison"

    titre = str(data.get("name") or "").strip()
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()
    description = str(data.get("description") or "").strip()

    # Photos depuis le JSON-LD
    photos: list[str] = []
    for ph in item.get("photo", []) or []:
        if isinstance(ph, dict):
            u = ph.get("url")
            if u:
                photos.append(u)
    if not photos:
        img = data.get("image")
        if isinstance(img, str) and img:
            photos.append(img)
    photos = photos[:PHOTOS_PER_CARD]

    # Terrain & chambres depuis le HTML (label : valeur)
    surface_terrain = _grab_m2(html, "Terrain")
    chambres = _grab_int(html, "Chambres") or _grab_int(html, "Chambre")

    return {
        "source": "maisonblanche",
        "url": url if url.startswith("http") else BASE_URL + url,
        "id_annonce": ref,
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
        "dpe": None,
        "agence": "Maison Blanche Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_jsonld(html: str) -> dict | None:
    m = re.search(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        return None
    try:
        obj = json.loads(m.group(1).strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(obj, list):
        obj = next((o for o in obj if isinstance(o, dict)
                    and str(o.get("@type", "")).lower() == "product"), None)
    if isinstance(obj, dict) and str(obj.get("@type", "")).lower() == "product":
        return obj
    return None


def _grab_m2(html: str, label: str) -> float | None:
    """Label-valeur Netty : 'Terrain<span> : </span>...<span>12 100 m²</span>'."""
    m = re.search(
        re.escape(label) + r"<span[^>]*>\xa0?:?\xa0?\s*:?\s*</span>.{0,80}?"
        r"([\d\s\xa0]+)\xa0?\s*m²",
        html,
        re.DOTALL,
    )
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if f > 0:
                return f
        except ValueError:
            pass
    return None


def _grab_int(html: str, label: str) -> int | None:
    m = re.search(
        re.escape(label) + r"<span[^>]*>\xa0?:?\xa0?\s*:?\s*</span>.{0,80}?"
        r"(\d{1,3})",
        html,
        re.DOTALL,
    )
    return int(m.group(1)) if m else None


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace("\xa0", "").replace(" ", "").replace(",", "."))
    except (ValueError, TypeError):
        return None


def _to_int(v) -> int | None:
    f = _to_float(v)
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
    print(f"\nTotal Maison Blanche: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['pieces'] or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

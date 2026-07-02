"""scrapers/immo_sarthois.py — Immo Sarthois (agence indépendante, Mayet 72)

Méthode : scrape_simple (httpx) — SSR via JSON-LD sur les pages détail.

Le site (plateforme Septeo/Netty, front React CSR) n'expose PAS la liste des
biens dans le HTML brut de /vente (rendu client). En revanche :
  • le sitemap https://www.immo-sarthois.fr/sitemap.xml liste TOUTES les URLs
    de biens : /vente/{slug-avec-CP},{REF}  (ex : .../mayet-72360,VM2862) — le
    CODE POSTAL est inscrit dans le slug ;
  • chaque page détail contient un <script type="application/ld+json"> de type
    Product entièrement SSR : name, description, image, offers.price,
    itemOffered (House : numberOfRooms, floorSize/value, address.postalCode +
    addressLocality, photo[]).

Stratégie filtre département : agence mono-dept (Sarthe 72), mais le sitemap
contient quelques biens hors-zone (showroom Savoie 73, Dubai…). On pré-filtre
sur le CP du slug PUIS on re-vérifie strictement postalCode[:2] ∈ départements
cibles depuis le JSON-LD → 0 fuite.

Type de bien : déduit du slug (on ne garde que maison/propriété/longère/ferme/
manoir/demeure/moulin/domaine/château ; on exclut appartement/terrain/fonds/
commerce/local/immeuble/parking/garage/bureau).

Pas de terrain ni DPE dans le JSON-LD liste → laissés à None (enrichis ensuite
par gallery.py sur la description complète).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx

from scrapers._base import HEADERS

BASE_URL = "https://www.immo-sarthois.fr"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
MAX_DETAILS = 200          # plafond de pages détail visitées
DETAIL_CONCURRENCY = 4     # requêtes détail en parallèle


# Types de bien (segment de slug) à conserver / exclure
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|"
    r"maison-de-village|maison-bourgeoise|maison-ancienne",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds-de-commerce|fonds|cave|box|chambre",
    re.IGNORECASE,
)

# Le HTML détail est volumineux (~800 Ko) et contient un gros state React inline
# qui fait dérailler le parseur HTML ; on extrait le JSON-LD Product par regex.
_LD_RE = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        try:
            urls = await _list_product_urls(client, departements)
        except Exception as e:
            print(f"[ImmoSarthois] Erreur sitemap : {e}")
            return []

        print(f"[ImmoSarthois] {len(urls)} biens candidats (slug en zone) à inspecter")
        urls = urls[:MAX_DETAILS]

        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
        seen_ids: set[str] = set()

        async def _one(url: str):
            async with sem:
                try:
                    bien = await _parse_detail(client, url, departements)
                except Exception:
                    return None
                await asyncio.sleep(0.3)
                return bien

        biens = await asyncio.gather(*[_one(u) for u in urls])

        for bien in biens:
            if not bien:
                continue
            # Filtre dept STRICT (re-vérifie le CP du JSON-LD)
            cp = bien.get("code_postal") or ""
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

    print(f"[ImmoSarthois] {len(results)} biens retenus")
    return results


async def _list_product_urls(
    client: httpx.AsyncClient, departements: set[str]
) -> list[str]:
    """Lit le sitemap et renvoie les URLs de biens maison/propriété en zone."""
    r = await client.get(SITEMAP_URL)
    r.raise_for_status()
    locs = re.findall(r"<loc>([^<]+)</loc>", r.text)

    urls: list[str] = []
    for loc in locs:
        # URL de bien = /vente/{slug-...-CP},{REF}
        if "/vente/" not in loc or "," not in loc:
            continue
        slug = loc.rsplit("/vente/", 1)[-1]
        slug_part = slug.split(",")[0]
        # Type de bien
        if _EXCLUDE_TYPE.search(slug_part) and not _KEEP_TYPE.search(slug_part):
            continue
        if not _KEEP_TYPE.search(slug_part):
            continue
        # Pré-filtre dept via le CP inscrit dans le slug (re-vérifié au détail)
        m = re.search(r"-(\d{5}),", slug)
        if m and m.group(1)[:2] not in departements:
            continue
        urls.append(loc)
    return urls


async def _parse_detail(
    client: httpx.AsyncClient, url: str, departements: set[str]
) -> dict | None:
    r = await client.get(url)
    if r.status_code != 200:
        return None

    ld = None
    for block in _LD_RE.findall(r.text):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            ld = data
            break
    if not ld:
        return None

    offers = ld.get("offers") or {}
    item = offers.get("itemOffered") or {}
    address = item.get("address") or {}

    code_postal = str(address.get("postalCode") or "").strip()
    if not code_postal or code_postal[:2] not in departements:
        return None
    dept = code_postal[:2]
    ville = str(address.get("addressLocality") or "").strip()

    # Référence depuis l'URL : ...,{REF}
    ref = url.rsplit(",", 1)[-1].strip() if "," in url else url

    # Type de bien depuis le slug
    slug_part = url.rsplit("/vente/", 1)[-1].split(",")[0]
    m_type = _KEEP_TYPE.search(slug_part)
    type_bien = (m_type.group(0) if m_type else "maison").replace("-", " ").lower()

    titre = (ld.get("name") or "").strip()
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    description = (ld.get("description") or "").strip()

    prix = _to_float(offers.get("price"))

    pieces = _to_int(item.get("numberOfRooms"))
    chambres_raw = item.get("numberOfBedrooms")
    chambres = _to_int(chambres_raw) if chambres_raw is not None else None

    surface = None
    floor = item.get("floorSize") or {}
    if isinstance(floor, dict):
        surface = _to_float(floor.get("value"))

    # Photos
    photos: list[str] = []
    raw_photos = item.get("photo")
    if isinstance(raw_photos, list):
        for ph in raw_photos:
            if isinstance(ph, dict):
                u = ph.get("url")
            else:
                u = ph
            if u:
                photos.append(u)
    elif isinstance(raw_photos, dict) and raw_photos.get("url"):
        photos.append(raw_photos["url"])
    # secours : image principale du Product
    if not photos and ld.get("image"):
        img = ld["image"]
        photos.append(img if isinstance(img, str) else img.get("url", ""))
    photos = [p for p in photos if p]

    return {
        "source": "immo_sarthois",
        "url": url,
        "id_annonce": ref,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Immo Sarthois",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _to_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(str(val).replace(",", ".").replace(" ", ""))
    except (ValueError, TypeError):
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
    print(f"\nTotal Immo Sarthois : {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
            f" — {len(b['photos'])} photos"
        )

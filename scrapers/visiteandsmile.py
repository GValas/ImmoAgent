"""scrapers/visiteandsmile.py — Visite And Smile (agence d'Orléans, plateforme Netty)

Méthode : scrape_simple (httpx) — SSR partiel via JSON-LD.

Plateforme Netty (front React CSR) : la liste /vente est rendue côté client
(JS-only) donc inexploitable en httpx. En revanche le **sitemap.xml** expose
toutes les fiches détail vente, et chaque page détail rend en SSR un bloc
JSON-LD (schema.org/Product) contenant l'essentiel : titre, pièces, surface,
ville, code postal, photos et **prix** (offers.price). Pas besoin de Playwright.

Découverte des biens : https://www.visiteandsmile.fr/sitemap.xml
  → on garde les URL /vente/{slug}-{ville}-{cp},{REF}
    avec REF préfixé par type : VM=maison, VA=appartement, VS=stationnement,
    VT=terrain, VI=immeuble. On ne conserve que VM (maisons) et VA (apparts).

Filtre département : le **code postal** est en suffixe du slug de chaque URL
(…-orleans-45000,VM1143). Aucun filtre serveur par département exploitable.
→ Post-filtre STRICT code_postal[:2] in departements (écarte p.ex. le bien 78
qui apparaît dans le sitemap : appartement-…-houilles-78800,VA2794).

Page détail (JSON-LD application/ld+json, @type Product) :
  - name                                   → titre
  - offers.price                           → prix (0 = "nous consulter")
  - offers.itemOffered.numberOfRooms       → pièces
  - offers.itemOffered.floorSize.value     → surface habitable (m²)
  - offers.itemOffered.address.postalCode  → code postal (filtre dept)
  - offers.itemOffered.address.addressLocality → ville
  - offers.itemOffered.photo[].url         → photos
Le bloc JSON-LD est injecté par react-helmet : sur un cache SSR « froid » il
peut manquer ponctuellement → on retente quelques fois la fiche.
Terrain et DPE ne sont pas dans le SSR → None.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx

BASE_URL = "https://www.visiteandsmile.fr"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
LD_RETRIES = 4          # le JSON-LD SSR (react-helmet) peut manquer ponctuellement
CONCURRENCY = 6
PHOTOS_PER_BIEN = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Préfixe de référence (dans l'URL) → type de bien conservé
_TYPE_BY_PREFIX = {
    "VM": "maison",
    "VA": "appartement",
}
# VS (stationnement), VT (terrain), VI (immeuble), LA/LP (location)… exclus.

_VENTE_DETAIL_RE = re.compile(r"/vente/[^,]+,(V[A-Z])\d+$")


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        urls = await _collect_detail_urls(client, departements)
        print(f"[VisiteAndSmile] {len(urls)} fiches vente (maison/appart) à visiter")

        sem = asyncio.Semaphore(CONCURRENCY)

        async def worker(url: str):
            async with sem:
                bien = await _scrape_detail(client, url)
                await asyncio.sleep(0.5)
                return bien

        raw = await asyncio.gather(*(worker(u) for u in urls))

    results: list[dict] = []
    for bien in raw:
        if not bien:
            continue
        cp = bien.get("code_postal") or ""
        # Post-filtre STRICT département — 0 fuite
        if not cp or cp[:2] not in departements:
            continue

        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        bien["departement"] = cp[:2]
        results.append(bien)

    vus = sorted({b["code_postal"][:2] for b in results})
    print(f"[VisiteAndSmile] {len(results)} biens retenus — départements vus : {vus}")
    return results


async def _collect_detail_urls(
    client: httpx.AsyncClient, departements: list[str]
) -> list[str]:
    """Liste les URL détail vente (maison/appart) dont le CP du slug est dans la zone."""
    try:
        r = await client.get(SITEMAP_URL)
    except Exception as e:
        print(f"[VisiteAndSmile] Erreur sitemap : {e}")
        return []
    if r.status_code != 200:
        print(f"[VisiteAndSmile] Sitemap status {r.status_code}")
        return []

    locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", r.text)
    urls: list[str] = []
    for loc in locs:
        m = _VENTE_DETAIL_RE.search(loc)
        if not m:
            continue
        if m.group(1) not in _TYPE_BY_PREFIX:
            continue
        # Pré-filtre département sur le CP du slug (sécurité + économie de requêtes)
        cp = _cp_from_slug(loc)
        if cp and cp[:2] not in departements:
            continue
        urls.append(loc)
    return urls


async def _scrape_detail(client: httpx.AsyncClient, url: str) -> dict | None:
    data = None
    for _ in range(LD_RETRIES):
        try:
            r = await client.get(url)
        except Exception:
            return None
        if r.status_code != 200:
            return None
        data = _extract_ld_product(r.text)
        if data is not None:
            break
        await asyncio.sleep(0.4)  # cache SSR « froid » : on laisse le temps au rendu
    if data is None:
        return None

    offers = data.get("offers", {}) or {}
    item = offers.get("itemOffered", {}) or {}
    addr = item.get("address", {}) or {}

    code_postal = str(addr.get("postalCode") or "").strip()
    ville = str(addr.get("addressLocality") or "").strip()

    prix = _to_number(offers.get("price"))
    if prix is not None and prix <= 0:
        prix = None  # 0 = "nous consulter"

    pieces = _to_int(item.get("numberOfRooms"))
    fs = item.get("floorSize", {}) or {}
    surface = _to_number(fs.get("value"))

    photos = []
    for ph in item.get("photo", []) or []:
        u = ph.get("url") if isinstance(ph, dict) else ph
        if u and isinstance(u, str) and not u.startswith("data:"):
            photos.append(u)
    if not photos:
        og = data.get("image")
        if isinstance(og, str) and og:
            photos.append(og)
    photos = photos[:PHOTOS_PER_BIEN]

    ref = _ref_from_url(url)
    type_bien = _TYPE_BY_PREFIX.get(ref[:2], "") if ref else ""

    titre = str(data.get("name") or "").strip()
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()
    description = str(data.get("description") or "").strip()

    return {
        "source": "visiteandsmile",
        "url": url,
        "id_annonce": ref or url,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,   # absent du SSR
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,               # absent du SSR
        "agence": "Visite And Smile",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_ld_product(html: str) -> dict | None:
    """Renvoie le 1er bloc JSON-LD @type Product, ou None s'il n'est pas (encore) rendu."""
    for blk in re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
    ):
        try:
            d = json.loads(blk)
        except Exception:
            continue
        if isinstance(d, list):
            d = next((x for x in d if isinstance(x, dict) and x.get("@type") == "Product"), None)
            if d is None:
                continue
        if isinstance(d, dict) and d.get("@type") == "Product":
            return d
    return None


def _cp_from_slug(url: str) -> str:
    """…-orleans-45000,VM1143 → '45000'"""
    m = re.search(r"-(\d{5}),V[A-Z]\d+$", url)
    return m.group(1) if m else ""


def _ref_from_url(url: str) -> str:
    """…,VM1143 → 'VM1143'"""
    m = re.search(r",(V[A-Z]\d+)$", url)
    return m.group(1) if m else ""


def _to_number(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(str(v).replace(",", "."))
        return f if f > 0 else (0.0 if f == 0 else None)
    except (ValueError, TypeError):
        return None


def _to_int(v) -> int | None:
    f = _to_number(v)
    return int(f) if f is not None and f > 0 else None


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
    print(f"\nTotal Visite And Smile : {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']} — {len(b['photos'])} photos"
        )

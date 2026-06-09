"""scrapers/immobiliercoteloire.py — Immobilier Côté Loire (agence familiale Orléans)

Méthode : scrape_simple (httpx) — sitemap + pages détail semi-SSR.

Particularité : la liste /vente est rendue en React CSR (HTML brut sans prix ni
biens), MAIS le sitemap.xml expose toutes les URL détail et chaque page détail
contient un bloc JSON-LD `Product` exploitable en httpx pur (pas de Playwright).

Découverte des biens : https://www.immobiliercoteloire.fr/sitemap.xml
  → on garde les <loc> sous /vente/ se terminant par une réf ",VM..." :
    /vente/{type}-{pieces}-pieces-{ville}-{cp},{REF}
  Le code postal est en suffixe de slug ET dans le JSON-LD → filtre dept trivial.
  Le sitemap est déjà 100 % département 45 (agence mono-département Orléans/Loiret).

Page détail — bloc <script type="application/ld+json"> @type Product :
  - name        : "Maison ancienne à vendre, 6 pièces - Orléans 45000"
  - description  : texte court
  - image        : photo principale (img.netty.immo)
  - offers.price : TOUJOURS 0 — le prix est injecté côté client uniquement.
                   → prix non récupérable en httpx. On laisse prix=None ; le
                     post-filtre prix_min/max n'exclut pas les biens à prix
                     inconnu (cohérent avec les autres scrapers du projet).
  - offers.itemOffered (House) :
      numberOfRooms, numberOfBathroomsTotal,
      floorSize.value (m² habitable),
      address.addressLocality / address.postalCode,
      photo[] (liste d'ImageObject .url)

Filtre département : post-filtre STRICT code_postal[:2] == dept (sitemap 100% 45).

Limites : prix indisponible (client-side) → pas de filtrage budget ; DPE absent
du JSON-LD. Petit inventaire (~25-35 biens), 100 % vente.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.immobiliercoteloire.fr"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
MAX_DETAILS = 200
PHOTOS_PER_BIEN = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# URL détail vente : /vente/{slug}-{cp},{REF}
_VENTE_DETAIL = re.compile(r"/vente/[^/]+-\d{5},[A-Za-z0-9]+$")

# Types exclus (on ne garde pas les appartements purs / locaux / terrains).
_EXCLUDE_TYPE = re.compile(
    r"\b(appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds)\b",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    surface_min = criteres.get("surface_min", 0)

    # Agence mono-département (45) : si 45 n'est pas demandé, rien à faire.
    if "45" not in departements:
        print("[CoteLoire] Dept 45 hors zone demandée → 0 annonce")
        return []

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        try:
            urls = await _list_detail_urls(client)
        except Exception as e:
            print(f"[CoteLoire] Erreur sitemap: {e}")
            return []

        print(f"[CoteLoire] {len(urls)} URL détail vente dans le sitemap")

        for url in urls[:MAX_DETAILS]:
            try:
                bien = await _scrape_detail(client, url, departements)
            except Exception as e:
                print(f"[CoteLoire] Erreur détail {url}: {e}")
                bien = None
            if not bien:
                continue

            # Post-filtre département STRICT
            if not bien["code_postal"] or bien["code_postal"][:2] not in departements:
                continue

            s = bien.get("surface") or 0
            if surface_min and s and s < surface_min:
                continue

            results.append(bien)
            await asyncio.sleep(0.5)

    print(f"[CoteLoire] {len(results)} annonces retenues")
    return results


async def _list_detail_urls(client: httpx.AsyncClient) -> list[str]:
    r = await client.get(SITEMAP_URL)
    r.raise_for_status()
    locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", r.text)
    return [u for u in locs if _VENTE_DETAIL.search(u)]


async def _scrape_detail(
    client: httpx.AsyncClient, url: str, departements: list[str]
) -> dict | None:
    # Réf depuis l'URL : ...-{cp},{REF}
    ref = url.rsplit(",", 1)[-1] if "," in url else url
    type_seg = url.split("/vente/", 1)[-1].split("-")[0:2]
    type_hint = "-".join(type_seg)
    if _EXCLUDE_TYPE.search(type_hint):
        return None

    r = await client.get(url)
    if r.status_code != 200:
        return None

    data = _extract_product_ld(r.text)
    if not data:
        return None

    item = (data.get("offers") or {}).get("itemOffered") or {}
    addr = item.get("address") or {}

    code_postal = str(addr.get("postalCode") or "").strip()
    ville = (addr.get("addressLocality") or "").strip()

    # Secours code postal depuis le slug d'URL si absent du JSON-LD
    if not re.fullmatch(r"\d{5}", code_postal):
        m = re.search(r"-(\d{5}),", url)
        code_postal = m.group(1) if m else ""

    dept = code_postal[:2] if code_postal else ""
    if dept not in departements:
        return None

    name = (data.get("name") or "").strip()
    description = (data.get("description") or "").strip()

    type_bien = _parse_type(name, type_hint)

    pieces = item.get("numberOfRooms")
    pieces = int(pieces) if isinstance(pieces, (int, float)) and pieces else None

    chambres = item.get("numberOfBedrooms")
    chambres = int(chambres) if isinstance(chambres, (int, float)) and chambres else None

    surface = None
    fs = item.get("floorSize") or {}
    fs_val = fs.get("value")
    if isinstance(fs_val, (int, float)) and fs_val:
        surface = float(fs_val)

    photos = _extract_photos(item, data)

    return {
        "source": "immobiliercoteloire",
        "url": url if url.startswith("http") else f"https://{url}",
        "id_annonce": ref,
        "titre": name[:150] or f"{type_bien.title()} {ville}".strip(),
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": None,  # prix injecté côté client → indisponible en httpx
        "photos": photos,
        "dpe": None,
        "agence": "Immobilier Côté Loire",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_product_ld(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for c in candidates:
            if isinstance(c, dict) and c.get("@type") == "Product":
                return c
    return None


def _parse_type(name: str, type_hint: str) -> str:
    low = (name + " " + type_hint).lower()
    if "château" in low or "chateau" in low:
        return "château"
    if "propriété" in low or "propriete" in low:
        return "propriété"
    if "appartement" in low:
        return "appartement"
    if "maison" in low:
        return "maison"
    # depuis le slug d'URL
    seg = type_hint.replace("-", " ").strip()
    return seg or "bien"


def _extract_photos(item: dict, data: dict) -> list[str]:
    photos: list[str] = []
    raw = item.get("photo") or []
    if isinstance(raw, dict):
        raw = [raw]
    for p in raw:
        if isinstance(p, dict):
            u = p.get("url")
        else:
            u = p
        if isinstance(u, str) and u.startswith("http"):
            photos.append(u)
    if not photos:
        img = data.get("image")
        if isinstance(img, str) and img.startswith("http"):
            photos.append(img)
        elif isinstance(img, list):
            photos.extend([u for u in img if isinstance(u, str) and u.startswith("http")])
    # dédup en conservant l'ordre
    seen: set[str] = set()
    uniq = []
    for u in photos:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:PHOTOS_PER_BIEN]


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
    print(f"\nTotal Côté Loire: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {len(b['photos'])} photos"
            f" — {b['type_bien']} — {b['ville']}"
        )

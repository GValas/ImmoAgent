"""scrapers/immobilier_amo.py — AMO Immobilier (agence indépendante d'Orléans)

Méthode : scrape_simple (httpx) — pas de Playwright.

Plateforme Netty (immobilier-amo.fr). La liste des biens est rendue côté client
(CSR React, 0 prix dans le HTML de la page liste), MAIS :
  - le **sitemap.xml** liste toutes les URL de détail vente
    (`/vente/{type}-...-{ville}-{cp},{REF}`) ;
  - chaque **page détail** contient un bloc JSON-LD `Product` en SSR avec
    TOUT le nécessaire (prix, pièces, surface, adresse locality + postalCode,
    photos). Le prix EST présent dans ce JSON-LD (contrairement à ce que laisse
    croire l'absence de prix sur la page liste).

Stratégie :
  1. GET sitemap.xml → garder les `<loc>` de détail vente
     (motif `/vente/...-{cp},{REF}` ; on écarte les URL de catégorie/ville).
  2. Filtre département via le CP en suffixe de slug (`...-45000,VA2022`) →
     post-filtre STRICT code_postal[:2] in departements (sitemap 100 % 45, mais
     on vérifie quand même → 0 fuite garantie).
  3. GET chaque page détail → parse le JSON-LD Product :
        offers.price, itemOffered.numberOfRooms, itemOffered.floorSize.value,
        itemOffered.address.{addressLocality,postalCode}, itemOffered.photo[].url,
        @type (Apartment/House/...). Pour les terrains, itemOffered == "" → on
        retombe sur le slug (type=terrain, surface depuis "...-NNN-m2-...").

Référence (id_annonce) : suffixe après la virgule du slug (VA2022, VT104...).
Type de bien : déduit de itemOffered.@type sinon du 1er segment du slug.

Couverture : Orléans + agglo, département 45 uniquement (~27 ventes, surtout
appartements/terrains ; quelques maisons selon le stock). Faible volume mais réel.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx

from scrapers._base import HEADERS

BASE_URL = "https://www.immobilier-amo.fr"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
PHOTOS_PER_CARD = 10
CONCURRENCY = 6


# URL de détail vente : se termine par "-{cp},{REF}" (ex : ...-45000,VA2022)
_DETAIL_RE = re.compile(r"/vente/.+-(\d{5}),([A-Z]{2}\d+)\s*$")

# Types à conserver (maisons / propriétés). Appartements, terrains, etc. exclus.
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|pavillon|corps-de-ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)

# Mapping @type schema.org → libellé FR
_SCHEMA_TYPE = {
    "Apartment": "appartement",
    "House": "maison",
    "SingleFamilyResidence": "maison",
    "Residence": "maison",
}


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        try:
            r = await client.get(SITEMAP_URL)
        except Exception as e:
            print(f"[AMO] Erreur sitemap : {e}")
            return results
        if r.status_code != 200:
            print(f"[AMO] Sitemap status {r.status_code}")
            return results

        # URL de détail + CP, post-filtrées par département cible
        targets: list[tuple[str, str, str]] = []  # (url, cp, ref)
        for loc in re.findall(r"<loc>(.*?)</loc>", r.text):
            m = _DETAIL_RE.search(loc.strip())
            if not m:
                continue
            cp, ref = m.group(1), m.group(2)
            if cp[:2] not in departements:
                continue
            targets.append((loc.strip(), cp, ref))

        print(f"[AMO] {len(targets)} annonces vente dans la zone (sitemap)")

        sem = asyncio.Semaphore(CONCURRENCY)

        async def fetch(url: str, cp: str, ref: str):
            async with sem:
                try:
                    bien = await _scrape_detail(client, url, cp, ref)
                except Exception as e:
                    print(f"[AMO] Erreur détail {ref}: {e}")
                    return None
                await asyncio.sleep(0.4)
                return bien

        biens = await asyncio.gather(*(fetch(u, cp, ref) for u, cp, ref in targets))

    for dept in departements:
        n = 0
        for bien in biens:
            if not bien:
                continue
            # Post-filtre STRICT : 0 fuite hors-département
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

            results.append(bien)
            n += 1
        if n:
            print(f"[AMO] Dept {dept}: {n} annonces")

    return results


async def _scrape_detail(
    client: httpx.AsyncClient, url: str, cp: str, ref: str
) -> dict | None:
    r = await client.get(url)
    if r.status_code != 200:
        return None
    html = r.text

    data = _extract_jsonld_product(html)

    offers = data.get("offers") if isinstance(data, dict) else None
    if not isinstance(offers, dict):
        offers = {}
    io = offers.get("itemOffered")
    if not isinstance(io, dict):
        io = {}

    # Type de bien : schema.org puis slug
    type_bien = _SCHEMA_TYPE.get(io.get("@type", ""))
    slug = url.rsplit("/", 1)[-1].split(",", 1)[0]
    if not type_bien:
        type_bien = _type_from_slug(slug)

    # On ne garde que maisons / propriétés
    if _EXCLUDE_TYPE.search(type_bien) and not _KEEP_TYPE.search(type_bien):
        return None
    if not _KEEP_TYPE.search(type_bien):
        return None

    titre = (data.get("name") or "").strip() if isinstance(data, dict) else ""
    if not titre:
        titre = type_bien.title()
    description = (data.get("description") or "").strip() if isinstance(data, dict) else ""

    # Prix
    prix = _to_float(offers.get("price"))

    # Adresse (locality + postalCode) — CP de secours = celui du slug
    addr = io.get("address") if isinstance(io.get("address"), dict) else {}
    ville = (addr.get("addressLocality") or "").strip()
    code_postal = (addr.get("postalCode") or "").strip() or cp
    if not ville:
        ville = _ville_from_slug(slug, cp)

    dept = code_postal[:2] if code_postal else cp[:2]

    # Pièces / surface
    pieces = _to_int(io.get("numberOfRooms"))
    # JSON-LD n'expose que numberOfBathroomsTotal (≠ chambres) → on laisse None
    chambres = None
    fs = io.get("floorSize")
    surface = None
    if isinstance(fs, dict):
        surface = _to_float(fs.get("value"))
    # Surface habitable de secours via slug "...-NNN-m2-..."
    if surface is None:
        surface = _surface_from_slug(slug)

    # Terrain : pas exposé en JSON-LD pour les maisons ; on tente le slug/description
    surface_terrain = None

    # Photos
    photos: list[str] = []
    for ph in io.get("photo", []) or []:
        if isinstance(ph, dict):
            src = ph.get("url")
        else:
            src = ph
        if src and isinstance(src, str) and not src.startswith("data:"):
            photos.append(src)
    if not photos and isinstance(data, dict):
        img = data.get("image")
        if isinstance(img, str) and img:
            photos.append(img)
    photos = photos[:PHOTOS_PER_CARD]

    # DPE (lettre) si présente dans le HTML
    dpe = _parse_dpe(html)

    return {
        "source": "immobilier_amo",
        "url": url,
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
        "dpe": dpe,
        "agence": "AMO Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_jsonld_product(html: str) -> dict:
    for block in re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
    ):
        try:
            d = json.loads(block.strip())
        except Exception:
            continue
        items = d if isinstance(d, list) else [d]
        for it in items:
            if isinstance(it, dict) and it.get("@type") == "Product":
                return it
    return {}


def _type_from_slug(slug: str) -> str:
    """'appartement-t4-4-pieces-orleans' → 'appartement' ; 'maison-...' → 'maison'."""
    first = slug.split("-", 1)[0]
    return first or "bien"


def _ville_from_slug(slug: str, cp: str) -> str:
    """Reconstruit grossièrement la ville depuis le slug (avant le CP)."""
    m = re.search(r"-([a-z-]+)-" + re.escape(cp) + r"$", slug)
    if m:
        return m.group(1).replace("-", " ").title()
    return ""


def _surface_from_slug(slug: str) -> float | None:
    m = re.search(r"-(\d+)-m2-", slug)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _to_float(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        cleaned = re.sub(r"[^\d.]", "", str(v))
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None


def _to_int(v) -> int | None:
    f = _to_float(v)
    return int(f) if f is not None else None


def _parse_dpe(html: str) -> str | None:
    """Cherche une lettre DPE A–G près d'un libellé 'DPE'/'consommation énergétique'."""
    m = re.search(
        r"(?i)(?:dpe|consommation\s+[ée]nerg[ée]tique)\D{0,30}\b([A-G])\b", html
    )
    return m.group(1).upper() if m else None


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
    print(f"\nTotal AMO Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

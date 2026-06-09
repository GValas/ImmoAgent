"""scrapers/tissier_immobilier.py — Agence Tissier (Cosne-Cours-sur-Loire, Nièvre 58)

Agence indépendante de Cosne-Cours-sur-Loire (58200) couvrant la Nièvre (58) et le
Cher (18) limitrophe (Sancerrois). Site SSR (CMS « Netty / Apimo ») : la page de
liste rend ses premières cartes directement dans le HTML brut → httpx pur.

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /fr/ventes   (page de liste)
              → AUCUN filtre dept côté serveur. Le CP n'est PAS exposé dans la carte
                (Netty n'affiche que la ville en h3). Filtre dept = POST-FILTRE STRICT
                via résolution ville → département par l'API publique geo.api.gouv.fr
                (mise en cache, 1 requête par commune distincte).

LIMITE CONNUE : seules les ~6 premières annonces sont rendues côté serveur ; le
reste de l'inventaire est chargé en JavaScript (back-office Apimo, endpoint
authentifié non rejouable en httpx pur). Le scraper récupère donc le sous-ensemble
SSR (stock réel mais partiel). Pour la totalité : Playwright. Le filtre dept reste
strict (0 fuite) sur ce sous-ensemble.

Cartes : li.property (data-property-id)
  - URL    : a[href]  → /fr/propriete/vente+{type}+{ville}+{slug}+{id}
  - Titre  : h2  → "Maison de ville, Cosne-Cours-sur-Loire"
  - Ville  : h3  → "Maison de ville - COSNE-SUR-LOIRE" (libellé majuscules)
  - Prix   : li.price > div  → "125 000 €"
  - Surface: li.area  > div  → "110 m²"
  - Photos : .slider img[data-src]

Type de bien : déduit du segment "vente+{type}+..." de l'URL ; on ne garde que
               maisons / propriétés / fermes…, on exclut appartements / terrains /
               locaux / commerces / immeubles.

Couverture : Nièvre (58) + Cher (18) limitrophe.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://immobiliertissiersa.com"
LIST_PATH = "/fr/ventes"
GEO_API = "https://geo.api.gouv.fr/communes"
PHOTOS_PER_CARD = 8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_KEEP_TYPE = re.compile(
    r"maison|villa|propri[eé]t[eé]|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|mas|g[iî]te|corps de ferme|pavillon",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|hangar|studio|loft|ensemble immobilier",
    re.IGNORECASE,
)

# Cache ville → (dept, cp) pour la session
_GEO_CACHE: dict[str, tuple[str, str]] = {}


def _normalize_ville(ville: str) -> str:
    """Développe les abréviations courantes (ST→Saint, STE→Sainte) pour fiabiliser
    la résolution geo.api.gouv.fr."""
    v = re.sub(r"\bSTE\b", "Sainte", ville, flags=re.IGNORECASE)
    v = re.sub(r"\bST\b", "Saint", v, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", v).strip()


async def _resolve_dept(client: httpx.AsyncClient, ville: str) -> tuple[str, str]:
    """Résout une commune en (codeDepartement, codePostal) via geo.api.gouv.fr."""
    ville = _normalize_ville(ville)
    key = ville.strip().lower()
    if not key:
        return "", ""
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]
    dept = cp = ""
    try:
        r = await client.get(
            GEO_API,
            params={
                "nom": ville,
                "fields": "codeDepartement,codesPostaux",
                "boost": "population",
                "limit": 1,
            },
            timeout=15,
        )
        if r.status_code == 200 and r.json():
            j = r.json()[0]
            dept = j.get("codeDepartement", "") or ""
            cps = j.get("codesPostaux") or []
            cp = cps[0] if cps else ""
    except Exception:
        pass
    _GEO_CACHE[key] = (dept, cp)
    return dept, cp


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        try:
            r = await client.get(f"{BASE_URL}{LIST_PATH}")
        except Exception as e:
            print(f"[TissierImmo] Erreur réseau : {e}")
            return []
        if r.status_code != 200:
            print(f"[TissierImmo] HTTP {r.status_code}")
            return []

        cards = BeautifulSoup(r.text, "html.parser").select("li.property")
        for card in cards:
            try:
                bien = await _parse_card(client, card)
            except Exception:
                continue
            if not bien:
                continue

            # POST-FILTRE STRICT — 0 fuite hors-zone
            if bien["departement"] not in departements:
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
            await asyncio.sleep(0.2)

    print(f"[TissierImmo] {len(results)} annonces (depts {sorted({b['departement'] for b in results}) or '∅'})")
    return results


async def _parse_card(client: httpx.AsyncClient, card) -> dict | None:
    link = card.find("a", href=True)
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else f"{BASE_URL}{href}"

    # /fr/propriete/vente+{type}+{ville}+{slug}+{id}
    m_id = re.search(r"\+(\d+)\s*$", href.rstrip("/"))
    id_annonce = m_id.group(1) if m_id else url

    # Type depuis le segment d'URL
    m_type = re.search(r"vente\+([a-zà-ÿ\-]+)\+", href, re.IGNORECASE)
    type_seg = m_type.group(1) if m_type else ""
    if _EXCLUDE_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg.replace("-", " ").strip() or "maison"

    h2 = card.find("h2")
    titre = h2.get_text(" ", strip=True) if h2 else ""

    # Ville : h2 "Type, Ville" puis fallback h3
    ville = ""
    if "," in titre:
        ville = titre.split(",", 1)[1].strip()
    if not ville:
        h3 = card.find("h3")
        if h3 and "-" in h3.get_text():
            ville = h3.get_text(" ", strip=True).rsplit("-", 1)[1].strip().title()
    if not ville:
        return None

    # Département via geo.api.gouv.fr (ville → dept, cp)
    dept, cp = await _resolve_dept(client, ville)
    if not dept:
        return None

    # Prix / surface
    price_el = card.select_one("li.price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")
    area_el = card.select_one("li.area")
    surface = _parse_surface(area_el.get_text(" ", strip=True) if area_el else "")

    photos = []
    for img in card.select(".slider img, img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "tissier_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Agence Tissier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    if v and v < 1000:
        return None
    return v


def _parse_surface(text: str) -> float | None:
    m = re.search(r"([\d\s\xa0]+(?:[.,]\d+)?)\s*m", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
        try:
            f = float(val)
            if 8 <= f <= 5000:
                return f
        except ValueError:
            pass
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
    print(f"\nTotal Agence Tissier: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal'] or b['departement']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

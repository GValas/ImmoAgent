"""scrapers/la_francaise_immobiliere.py — La Française Immobilière (LFI)

Réseau d'agences (groupe Pigeault) implanté à Rennes et sa périphérie —
Ille-et-Vilaine (35) essentiellement, avec quelques communes limitrophes
(22/56). Site WordPress + catalogue immo-facile.com, rendu SSR (httpx pur).

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /achat/            (page 1)
              /achat/page/{N}/   (pagination, ~57 pages observées)
              → pas de filtre département côté serveur (réseau mono-zone 35).

Cartes : div.card.card-annonce
  - URL    : a[href]                        → /achat/vente-{type}-{tN}-{ville}-{id}/
  - Titre  : h2.card-title                  → "Vente Maison T6 - Janzé"
  - Ville  : p.city span                    → "Janzé"
  - Type   : span.type_bien                 → "T6" (nb pièces) — le vrai type est dans l'URL
  - Surface: span.surface                   → "84 m²"
  - Prix   : span.prix                       → "345 510 €"
  - Photos : div.card-img-top--slide img[data-lazy-src] / noscript img[src]

Filtre département : les cartes n'exposent PAS le code postal. La ville est
fiable ; on la résout en (code_postal, code_departement) via l'API publique
geo.api.gouv.fr (gratuite, sans clé, déjà utilisée pour la géoloc IGN). Le
post-filtre STRICT garde uniquement les biens dont le département résolu est
dans la zone cible → 0 fuite. Cache mémoire par ville pour limiter les appels.

Type de bien : déduit du segment d'URL (vente-{maison|appartement|...}-tN-...).
On ne garde que maisons / propriétés (les appartements sont exclus).

Couverture : réseau mono-département (35). Pour les départements du grand
Val-de-Loire / Ouest actuellement ciblés (72, 28, 45, 89, 49, 37, 36, 18,
58, 41, 53) → 0 stock attendu. Scraper fonctionnel, à réactiver / utile si la
zone cible inclut l'Ille-et-Vilaine.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.la-francaise-immobiliere.fr"
GEO_API = "https://geo.api.gouv.fr/communes"
MAX_PAGES = 60
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien (segment d'URL) à conserver : maisons / propriétés / fermes...
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|cave|box|programme|neuf",
    re.IGNORECASE,
)

# Cache mémoire ville (normalisée) -> (code_postal, code_departement)
_GEO_CACHE: dict[str, tuple[str | None, str | None]] = {}


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = BASE_URL + "/achat/" if page == 1 else f"{BASE_URL}/achat/page/{page}/"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[LFI] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.card.card-annonce")
            if not cards:
                break

            kept_on_page = 0
            for card in cards:
                try:
                    bien = await _parse_card(client, card, departements)
                except Exception:
                    continue
                if not bien:
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
                kept_on_page += 1

            await asyncio.sleep(0.6)

    print(f"[LFI] Total retenu (depts {departements}): {len(results)} annonces")
    return results


async def _parse_card(
    client: httpx.AsyncClient, card, departements: list[str]
) -> dict | None:
    link = card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href or "/achat/vente-" not in href:
        # certains liens sont des images : chercher un lien d'annonce
        for a in card.select("a[href]"):
            if "/achat/vente-" in a.get("href", ""):
                href = a["href"]
                break
    if not href or "/achat/vente-" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # /achat/vente-{type}-{tN}-{ville}-{id}/
    slug = href.rstrip("/").split("/")[-1]
    m_type = re.match(r"vente-([a-zà-ÿ\-]+?)-t\d", slug, re.IGNORECASE)
    type_seg = m_type.group(1) if m_type else ""
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg.replace("-", " ").strip() or "maison"

    # id_annonce : dernier groupe numérique du slug
    m_id = re.search(r"(\d{5,})$", slug)
    id_annonce = m_id.group(1) if m_id else url

    # Ville
    city_el = card.select_one("p.city span") or card.select_one("p.city")
    ville = city_el.get_text(" ", strip=True) if city_el else ""
    ville = re.sub(r"\s+", " ", ville).strip()
    if not ville:
        return None

    # Résolution ville -> (code_postal, departement) via geo.api.gouv.fr
    code_postal, dept = await _resolve_commune(client, ville)

    # Post-filtre STRICT : on n'accepte que les départements cibles (0 fuite)
    if dept not in departements:
        return None

    # Titre
    title_el = card.select_one("h2.card-title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    titre = re.sub(r"\s+", " ", titre).strip()
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Pièces (span.type_bien = "T4")
    pieces = None
    tb_el = card.select_one("span.type_bien")
    if tb_el:
        m = re.search(r"T\s*(\d+)", tb_el.get_text(" ", strip=True), re.IGNORECASE)
        if m:
            pieces = int(m.group(1))
    if pieces is None:
        m = re.search(r"-t(\d+)-", slug, re.IGNORECASE)
        if m:
            pieces = int(m.group(1))

    # Surface
    surface = None
    surf_el = card.select_one("span.surface")
    if surf_el:
        surface = _parse_surface(surf_el.get_text(" ", strip=True))

    # Prix
    prix = None
    price_el = card.select_one("span.prix")
    if price_el:
        prix = _parse_price(price_el.get_text(" ", strip=True))

    # Photos
    photos: list[str] = []
    for img in card.select("div.card-img-top--slide img, noscript img"):
        src = img.get("data-lazy-src") or img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "la_francaise_immobiliere",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal or "",
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "La Française Immobilière",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _resolve_commune(
    client: httpx.AsyncClient, ville: str
) -> tuple[str | None, str | None]:
    """Ville -> (code_postal, code_departement) via geo.api.gouv.fr (caché)."""
    key = ville.lower().strip()
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]

    cp = dept = None
    try:
        r = await client.get(
            GEO_API,
            params={
                "nom": ville,
                "fields": "codesPostaux,codeDepartement",
                "boost": "population",
                "limit": 1,
            },
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                dept = data[0].get("codeDepartement")
                cps = data[0].get("codesPostaux") or []
                cp = cps[0] if cps else None
    except Exception:
        pass

    _GEO_CACHE[key] = (cp, dept)
    return cp, dept


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text.replace("\xa0", ""))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    m = re.search(r"([\d\s\xa0]+)\s*m", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
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
    print(f"\nTotal La Française Immobilière: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

"""scrapers/_netty.py — Socle partagé pour les agences sur moteur Netty/Modelo.

De nombreuses agences indépendantes de la zone utilisent la plateforme
**Netty.immo / Modelo** (React côté client). Leurs pages /vente et les fiches
détail sont rendues en JS → vides en httpx pur. MAIS le **sitemap.xml** est servi
en SSR et liste TOUTES les fiches, avec le **code postal dans le slug** :

  Format « classique » :  /vente/{type}-...-{ville}-{CP},{REF}
      ex : /vente/maison-ancienne-7-pieces-nevers-58000,VM350
  Format « hex moderne » : /fr/vente/{type}-...-{ville}-{CP}/{HASH}
      ex : /fr/vente/maison-7-pieces-la-ferte-loupiere-89110/68F13A941855D730

Dans les deux cas le **CP (5 chiffres) précède immédiatement le séparateur**
(`,REF` ou `/HASH`) → filtre département 100 % fiable via CP[:2], aucun géocodage.

Le <head> SSR de la fiche (react-helmet) expose og:title / og:description /
og:image (CDN img.netty.immo) → on enrichit titre/description/photo en best-effort.
prix/surface/terrain restent souvent indisponibles en SSR (détail CSR) ; les
filtres du pipeline ignorent les champs manquants.

Référence historique : scrapers/xavierm_immobilier.py (1er pilote de ce socle).

Usage côté scraper concret :
    from scrapers._netty import netty_search
    async def search(criteres): return await netty_search(criteres, BASE_URL,
        source="monid", agence="Mon Agence")
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_int

MAX_DETAIL_FETCH = 60   # plafond de pages détail enrichies (politesse)
DETAIL_SLEEP = 0.4      # s entre deux fetch détail

# Types résidentiels conservés (1er segment du slug de fiche).
_KEEP_TYPE = re.compile(
    r"^(maison|propriete|propriété|longere|longère|ferme|villa|manoir|chateau|"
    r"château|moulin|demeure|corps-de-ferme|pavillon|gentilhommiere|"
    r"gentilhommière|domaine|grange|fermette|chalet)",
    re.IGNORECASE,
)
# Catégories explicitement non-résidentielles (exclues).
_EXCLUDE_TYPE = re.compile(
    r"^(appartement|immeuble|immobilier-pro|fonds-de-commerce|terrain|garage|"
    r"local|parking|bureau|commerce|stationnement|viager)",
    re.IGNORECASE,
)

# Fiche format classique : /vente/{slug},{REF}  (REF = 2 lettres + chiffres).
_DETAIL_CLASSIC = re.compile(r"/vente/(?P<slug>[a-z0-9\-]+),(?P<ref>[A-Z]{2}\d+)$")
# Fiche format hex : /fr/vente/{slug}/{HASH}  (HASH = ≥16 caractères hex).
_DETAIL_HEX = re.compile(
    r"/(?:fr/)?vente/(?P<slug>[a-z0-9\-]+)/(?P<ref>[0-9A-Fa-f]{16,})$"
)


def _parse_detail_url(loc: str):
    """(type, ville, code_postal, ref) depuis une <loc> de fiche, ou None.

    Gère les deux formats (classique ,REF et hex /HASH). Filtre les types
    non-résidentiels. Le CP est toujours l'avant-dernier segment du slug.
    """
    m = _DETAIL_CLASSIC.search(loc) or _DETAIL_HEX.search(loc)
    if not m:
        return None
    slug, ref = m.group("slug"), m.group("ref")

    if _EXCLUDE_TYPE.match(slug) or not _KEEP_TYPE.match(slug):
        return None

    cp_m = re.search(r"-(\d{5})$", slug)
    if not cp_m:
        return None
    code_postal = cp_m.group(1)

    type_bien = slug.split("-")[0].lower()

    core = slug[: cp_m.start()]            # tout avant "-{CP}"
    parts = core.split("-pieces-", 1)
    ville_slug = parts[1] if len(parts) == 2 else core.rsplit("-", 1)[-1]
    ville = ville_slug.replace("-", " ").title()

    return type_bien, ville, code_postal, ref


def _meta(soup, prop: str) -> str:
    el = soup.find("meta", attrs={"property": prop}) or soup.find(
        "meta", attrs={"name": prop}
    )
    return (el.get("content") or "").strip() if el else ""


def _photo_from_og(soup) -> list[str]:
    og = soup.find("meta", attrs={"property": "og:image"})
    src = (og.get("content") or "").strip() if og else ""
    return [src] if src.startswith("http") else []


async def _enrich(client, url: str) -> dict:
    """Best-effort : titre/description/photo depuis le <head> SSR. Ne lève jamais."""
    r = await get_with_retry(client, url)
    if r is None or r.status_code != 200:
        return {}
    try:
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return {}
    titre = _meta(soup, "og:title") or (
        soup.title.string.strip() if soup.title and soup.title.string else ""
    )
    description = _meta(soup, "og:description") or _meta(soup, "description")
    return {
        "titre": titre,
        "description": description,
        "photos": _photo_from_og(soup),
    }


async def netty_search(
    criteres: dict, base_url: str, source: str, agence: str,
    label: str | None = None,
) -> list[dict]:
    """Scrape un site Netty/Modelo via son sitemap.xml. 0 fuite (post-filtre CP[:2]).

    base_url : ex "https://www.topaze-immobilier.com" (sans / final)
    source   : id du scraper (= clé sources.yaml)
    agence   : nom affiché de l'agence
    """
    label = label or agence
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    if not departements:
        return []

    sitemap_url = f"{base_url}/sitemap.xml"
    results: list[dict] = []
    seen: set[str] = set()

    async with make_client() as client:
        r = await get_with_retry(client, sitemap_url)
        if r is None or r.status_code != 200:
            print(f"[{label}] Sitemap inaccessible (status={getattr(r, 'status_code', None)})")
            return []

        locs = re.findall(r"<loc>(.*?)</loc>", r.text)
        candidates = []
        for loc in locs:
            parsed = _parse_detail_url(loc.strip())
            if not parsed:
                continue
            type_bien, ville, code_postal, ref = parsed
            dept = code_postal[:2]
            if dept not in departements:          # POST-FILTRE DEPT STRICT — 0 fuite
                continue
            if ref in seen:
                continue
            seen.add(ref)
            candidates.append((loc.strip(), type_bien, ville, code_postal, ref, dept))

        print(f"[{label}] {len(candidates)} biens résidentiels dans la zone (sitemap)")

        for i, (url, type_bien, ville, code_postal, ref, dept) in enumerate(candidates):
            pieces = parse_int(r"-(\d+)-pieces-", url)
            titre = f"{type_bien.title()} {pieces} pièces {ville} ({code_postal})".replace(
                " None ", " "
            )
            description = ""
            photos: list[str] = []

            if i < MAX_DETAIL_FETCH:
                enr = await _enrich(client, url)
                if enr.get("titre"):
                    titre = enr["titre"]
                if enr.get("description"):
                    description = enr["description"]
                if enr.get("photos"):
                    photos = enr["photos"]
                await asyncio.sleep(DETAIL_SLEEP)

            results.append({
                "source": source,
                "url": url,
                "id_annonce": ref,
                "titre": titre[:150],
                "type_bien": type_bien,
                "description": description[:1200],
                "departement": dept,
                "ville": ville[:80],
                "code_postal": code_postal,
                "surface": None,
                "surface_terrain": None,
                "pieces": pieces,
                "chambres": None,
                "prix": None,
                "photos": photos,
                "dpe": None,
                "agence": agence,
            })

    print(f"[{label}] Total: {len(results)} biens")
    return results

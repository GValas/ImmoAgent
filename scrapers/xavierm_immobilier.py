"""scrapers/xavierm_immobilier.py — Xavier M Immobilier (Marzy / Nevers, 58)

Méthode : scrape_simple (httpx) — SSR via le **sitemap.xml** (la liste /vente et les
pages détail sont rendues côté client / React-Netty, donc vides en httpx ; le
sitemap, lui, est servi en SSR et liste TOUTES les annonces).

Agence indépendante (plateforme Netty/Modelo) couvrant surtout la Nièvre (58) et
un débord sur le Cher (18). Pas de pagination : un seul GET sitemap suffit.

URL pattern (source de vérité) :
    https://www.xaviermimmobilier.fr/sitemap.xml
    → <loc> de détail de la forme :
      /vente/{type}-...-{pieces}-pieces-{ville-slug}-{CP},{REF}
      ex : /vente/maison-ancienne-7-pieces-nevers-58000,VM350
    Le **code postal** est l'avant-dernier segment du slug (juste avant ",REF") →
    filtre département 100 % fiable via CP[:2], aucun géocodage requis.

Stratégie filtre dept (0 fuite) :
    CP extrait du slug, departement = CP[:2], POST-FILTRE STRICT
    CP[:2] ∈ criteres['departements']. Types non-résidentiels (1er segment du slug
    + segment /vente/{cat}) exclus : appartement, immeuble, immobilier-pro,
    fonds-de-commerce, terrain, garage, local, parking, bureau, commerce.

Enrichissement détail : les pages détail sont CSR (prix/surface absents du HTML),
mais leur <head> SSR (react-helmet) fournit og:title, description et og:image
(CDN img.netty.immo). On fetch chaque détail (best-effort, plafonné, sleep poli)
pour récupérer titre/description/photo propres ; en cas d'échec on retombe sur les
infos reconstruites depuis le slug. prix/surface restent None (indisponibles en
SSR) — les filtres du pipeline ignorent les champs manquants.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_int

BASE_URL = "https://www.xaviermimmobilier.fr"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
MAX_DETAIL_FETCH = 60   # plafond de pages détail enrichies (politesse)
DETAIL_SLEEP = 0.4      # s entre deux fetch détail

# Types résidentiels à conserver (1er segment du slug de détail).
_KEEP_TYPE = re.compile(
    r"^(maison|propriete|propriété|longere|longère|ferme|villa|manoir|chateau|"
    r"château|moulin|demeure|corps-de-ferme)",
    re.IGNORECASE,
)
# Catégories explicitement non-résidentielles (exclues même si le slug commence
# par un mot ambigu).
_EXCLUDE_TYPE = re.compile(
    r"^(appartement|immeuble|immobilier-pro|fonds-de-commerce|terrain|garage|"
    r"local|parking|bureau|commerce)",
    re.IGNORECASE,
)

# URL détail : /vente/{slug},{REF}  où REF = 2 lettres + chiffres (VM350, VA1908…)
_DETAIL_RE = re.compile(r"/vente/(?P<slug>[a-z0-9\-]+),(?P<ref>[A-Z]{2}\d+)$")


def _parse_detail_url(loc: str):
    """Extrait (type, ville, code_postal, ref) depuis une <loc> de détail.

    ex : .../vente/maison-ancienne-7-pieces-nevers-58000,VM350
    → ("maison", "Nevers", "58000", "VM350")  ;  None si non-détail ou non-résidentiel.
    """
    m = _DETAIL_RE.search(loc)
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

    # Ville = segment entre "...-pieces-" (ou début) et "-{CP}".
    core = slug[: cp_m.start()]            # tout avant "-{CP}"
    parts = core.split("-pieces-", 1)
    ville_slug = parts[1] if len(parts) == 2 else core.rsplit("-", 1)[-1]
    ville = ville_slug.replace("-", " ").title()

    return type_bien, ville, code_postal, ref


def _photo_from_og(soup) -> list[str]:
    og = soup.find("meta", attrs={"property": "og:image"})
    src = (og.get("content") or "").strip() if og else ""
    return [src] if src.startswith("http") else []


def _meta(soup, prop: str) -> str:
    el = soup.find("meta", attrs={"property": prop}) or soup.find(
        "meta", attrs={"name": prop}
    )
    return (el.get("content") or "").strip() if el else ""


async def _enrich(client, url: str) -> dict:
    """Best-effort : récupère titre/description/photo depuis le <head> SSR du détail.
    Ne lève jamais ; renvoie {} en cas d'échec."""
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


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    if not departements:
        return []

    results: list[dict] = []
    seen: set[str] = set()

    async with make_client() as client:
        r = await get_with_retry(client, SITEMAP_URL)
        if r is None or r.status_code != 200:
            print(f"[XavierM] Sitemap inaccessible (status={getattr(r, 'status_code', None)})")
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

        print(f"[XavierM] {len(candidates)} biens résidentiels dans la zone (sitemap)")

        for i, (url, type_bien, ville, code_postal, ref, dept) in enumerate(candidates):
            # pièces depuis le slug ("...-7-pieces-...")
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
                "source": "xavierm_immobilier",
                "url": url,
                "id_annonce": ref,
                "titre": titre[:150],
                "type_bien": type_bien,
                "description": description[:1200],
                "departement": dept,
                "ville": ville[:80],
                "code_postal": code_postal,
                "surface": None,            # absent du SSR (détail CSR)
                "surface_terrain": None,
                "pieces": pieces,
                "chambres": None,
                "prix": None,               # absent du SSR (détail CSR)
                "photos": photos,
                "dpe": None,
                "agence": "Xavier M Immobilier",
            })

    print(f"[XavierM] Total: {len(results)} biens")
    return results


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Xavier M Immobilier")

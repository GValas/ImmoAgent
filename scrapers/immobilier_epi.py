"""scrapers/immobilier_epi.py — Immobilier EPI (agences Saumur / Richelieu / Thouars)

Méthode : scrape_simple (httpx) — SSR WordPress (thème Avada + Ajax Load More).
Site : www.immobilier-epi.com (HTTP only — le HTTPS redirige vers dev.* en 503,
       on force donc http://). Réseau d'agences du Saumurois / Loudunais / Thouarsais
       / Richelais : couvre principalement Maine-et-Loire (49) et Indre-et-Loire (37)
       dans la zone cible, plus du 79/86 hors zone (écarté).

Stratégie :
  - La page liste /jachete/ charge les biens en AJAX (pas dans le HTML brut), MAIS le
    SITEMAP `bien-sitemap.xml` liste TOUTES les fiches (~150) en SSR, avec la VILLE
    dans le slug : /bien/{ville-slug}-{type}/{id}/.
  - Aucun code postal n'est présent ni dans la liste ni dans la fiche (seul le nom de
    commune apparaît). On résout donc VILLE → CP/département via l'API publique BAN
    (api-adresse.data.gouv.fr, type=municipality). PRÉ-FILTRE : on ne fetch la fiche
    que si le dept BAN ∈ départements cibles (économie de requêtes).
  - POST-FILTRE STRICT : code_postal[:2] (issu BAN) doit ∈ départements cibles → 0
    fuite (Thouars 79 / Loudun 86 écartés).
Fiche (SSR) : h1 = ville, .prix_bien = prix HAI, blocs surface (NN.Nm²) / pièces,
  .content_energie → DPE (classe via .dpe-conso-en.lettre-X), galerie photos.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE = "https://www.immobilier-epi.com"
SITEMAP = f"{BASE}/bien-sitemap.xml"
BAN_URL = "https://api-adresse.data.gouv.fr/search/"
# Centre géographique du réseau EPI (Saumur) — biaise le géocodage BAN vers les
# communes locales et lève les homonymies (« Arçay » → 86 près de Loudun, pas le
# Cher 18 ; « Brie » → 49, pas l'Aisne…).
EPI_LAT, EPI_LON = 47.26, -0.08

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# suffixes de type de bien à retirer du slug pour isoler la ville
_TYPE_SUFFIX = re.compile(
    r"-(maison|appartement|immeuble|terrain|local|propriete|fonds|bureau|"
    r"parking|grange|chateau|longere|ferme|hangar|garage|commerce|batiment|"
    r"moulin|t\d)(-.*)?$"
)


def _ville_from_slug(slug: str) -> str:
    """`saumur-appartement-t4` → `saumur` ; `montreuil-bellay-maison-ancienne` →
    `montreuil bellay`."""
    s = _TYPE_SUFFIX.sub("", slug)
    return s.replace("-", " ").strip()


async def _ville_to_cp(client: httpx.AsyncClient, ville: str,
                       cache: dict[str, str | None]) -> str | None:
    """VILLE → code postal via l'API BAN (type=municipality). Mémoïsé."""
    if ville in cache:
        return cache[ville]
    cp = None
    try:
        r = await client.get(BAN_URL, params={"q": ville, "type": "municipality",
                                              "limit": 1, "lat": EPI_LAT,
                                              "lon": EPI_LON}, timeout=12)
        if r.status_code == 200:
            feats = r.json().get("features") or []
            if feats:
                cp = feats[0]["properties"].get("postcode")
    except Exception:
        cp = None
    cache[ville] = cp
    return cp


def _parse_fiche(html: str, url: str, id_annonce: str, ville: str,
                 cp: str, dept: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.find("h1")
    ville_aff = h1.get_text(" ", strip=True) if h1 else ville.title()

    prix = None
    pel = soup.select_one(".prix_bien")
    if pel:
        cleaned = re.sub(r"[^\d]", "", pel.get_text())
        prix = float(cleaned) if cleaned else None

    # bloc bien : surface habitable + pièces dans le conteneur autour du prix
    surface = pieces = None
    txt = soup.get_text(" ", strip=True)
    msurf = re.search(r"(\d{1,4}(?:[.,]\d+)?)\s*m²", txt)
    # éviter de prendre un "1m²" parasite : surface plausible >= 9
    for m in re.finditer(r"(\d{1,4}(?:[.,]\d+)?)\s*m²", txt):
        v = float(m.group(1).replace(",", "."))
        if 9 <= v <= 5000:
            surface = v
            break

    og = soup.find("meta", property="og:title")
    type_lib = og.get("content", "") if og else ""
    type_bien = "maison"
    low = (type_lib + " " + url).lower()
    if "appartement" in low:
        type_bien = "appartement"
    elif "immeuble" in low:
        type_bien = "immeuble"
    elif "terrain" in low:
        type_bien = "terrain"
    elif "propriete" in low or "château" in low or "chateau" in low:
        type_bien = "propriete"

    ogd = soup.find("meta", property="og:description")
    description = ogd.get("content", "") if ogd else ""

    # pièces depuis description / titre (« T4 », « 3 chambres »)
    mp = re.search(r"\bT(\d)\b", type_lib + " " + url, re.IGNORECASE)
    if mp:
        pieces = int(mp.group(1))

    dpe = None
    dpe_el = soup.select_one(".dpe-conso-en[class*=lettre-]")
    if dpe_el:
        ml = re.search(r"lettre-([a-g])", " ".join(dpe_el.get("class", [])), re.IGNORECASE)
        if ml:
            dpe = ml.group(1).upper()

    photos = []
    for img in soup.select("img"):
        src = img.get("data-src") or img.get("src") or ""
        if "/uploads/" in src and src not in photos:
            photos.append(src)
    photos = photos[:15]

    return {
        "source": "immobilier_epi",
        "url": url,
        "id_annonce": id_annonce,
        "titre": (type_lib or f"{type_bien} à {ville_aff}")[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville_aff[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Immobilier EPI",
    }


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)
    results: list[dict] = []
    per_dept: dict[str, int] = {}

    async with httpx.AsyncClient(headers=HEADERS, timeout=25,
                                 follow_redirects=True, verify=False) as client:
        try:
            sm = await client.get(SITEMAP)
        except Exception as e:
            print(f"[EPI] Sitemap injoignable : {e}")
            return results
        if sm.status_code != 200:
            print(f"[EPI] Sitemap status {sm.status_code}")
            return results

        urls = re.findall(r"<loc>(https?://[^<]*?/bien/[^<]+)</loc>", sm.text)
        print(f"[EPI] {len(urls)} fiches au sitemap")

        cp_cache: dict[str, str | None] = {}
        # 1) pré-filtre par géocodage ville → dept cible
        retained: list[tuple[str, str, str, str, str]] = []  # url,id,ville,cp,dept
        for u in urls:
            m = re.search(r"/bien/([a-z0-9-]+)/(\d+)/?$", u)
            if not m:
                continue
            slug, id_annonce = m.group(1), m.group(2)
            ville = _ville_from_slug(slug)
            cp = await _ville_to_cp(client, ville, cp_cache)
            if not cp:
                continue
            dept = cp[:2]
            if dept not in departements:
                continue
            retained.append((u, id_annonce, ville, cp, dept))
            await asyncio.sleep(0.05)

        print(f"[EPI] {len(retained)} fiches en zone cible (pré-filtre BAN)")

        # 2) fetch + parse des fiches retenues
        seen: set[str] = set()
        for u, id_annonce, ville, cp, dept in retained:
            if id_annonce in seen:
                continue
            seen.add(id_annonce)
            try:
                r = await client.get(u)
                if r.status_code != 200:
                    continue
                bien = _parse_fiche(r.text, u, id_annonce, ville, cp, dept)
            except Exception:
                continue
            if not bien:
                continue
            # POST-FILTRE STRICT département
            if not bien["code_postal"] or bien["code_postal"][:2] not in departements:
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
            per_dept[dept] = per_dept.get(dept, 0) + 1
            await asyncio.sleep(0.3)

    for d in sorted(per_dept):
        print(f"[EPI] Dept {d}: {per_dept[d]} annonces")
    return results


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    biens = asyncio.run(search({
        "departements": criteres.departements,
        "prix_max": criteres.prix_max,
        "prix_min": getattr(criteres, "prix_min", 0),
        "surface_min": criteres.surface_min,
    }))
    print(f"\nTotal EPI: {len(biens)} annonces")
    depts = sorted({str(b.get("code_postal") or "")[:2] for b in biens if b.get("code_postal")})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(f"  [{b.get('code_postal')}] {str(b.get('titre'))[:50]} — "
              f"{b.get('prix')}€ — {b.get('surface') or '?'}m² — {b.get('ville')}")

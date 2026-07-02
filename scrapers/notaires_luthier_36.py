"""scrapers/notaires_luthier_36.py — SELARL LUTHIER & PENIN-MAILLET, notaires (Buzançais, 36)

Méthode : scrape_simple (httpx) — SSR HTML, plateforme « notariat.services ».
Site : https://www.luthier-notaires-buzancais.fr

Stratégie :
  1. La page d'index /annonces-immobilieres.html liste les pages communales
     /annonces-immobilieres/recherche/{insee}/{ville}-{cp}.html (SSR, pas de JS).
  2. Chaque page communale liste TOUS les biens de la commune sous forme de
     liens détail /annonces-immobilieres/annonce/{ref}/{slug}.html.
  3. Le slug encode déjà ville, CP, surface et pièces
     (ex: maison-a-vendre-saint-genou-36500-139m2-4pieces.html).
  4. La page détail fournit le prix (.product-price), le DPE, la description
     (meta description) et les photos (photos.notariat.services/photos/...).

Filtre département : ce notaire couvre essentiellement l'Indre (36, in-zone) avec
de rares biens hors-zone (ex: 21). Post-filtre STRICT sur code_postal[:2] → 0 fuite.

Type de bien : déduit du slug. On ne garde que maisons / propriétés / fermes /
demeures (exclut terrain, appartement, garage/parking, fonds de commerce, immeuble).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.luthier-notaires-buzancais.fr"
INDEX_URL = f"{BASE_URL}/annonces-immobilieres.html"
PHOTOS_PER_CARD = 12
DETAIL_CONCURRENCY = 4


# Types (depuis le slug) à conserver : maisons / propriétés / fermes…
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|"
    r"maison-de-village|maison-de-ville|maison-de-campagne|pavillon",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|bois-etang|loisirs",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1. Pages communales depuis l'index
        try:
            commune_urls = await _list_communes(client)
        except Exception as e:
            print(f"[LuthierNotaires36] Erreur index: {e}")
            return results

        # 2. Liens détail par commune
        detail_urls: set[str] = set()
        for cu in commune_urls:
            try:
                detail_urls |= await _list_details(client, cu)
            except Exception as e:
                print(f"[LuthierNotaires36] Erreur commune {cu}: {e}")
            await asyncio.sleep(0.3)

        print(f"[LuthierNotaires36] {len(detail_urls)} annonces détail trouvées")

        # 3. Pré-filtre département + type sur le slug (évite des fetchs inutiles)
        candidates: list[str] = []
        for du in detail_urls:
            cp = _cp_from_slug(du)
            if cp and cp[:2] not in departements:
                continue
            type_seg = _type_from_slug(du)
            if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
                continue
            if not _KEEP_TYPE.search(type_seg):
                continue
            candidates.append(du)

        # 4. Fetch des pages détail (concurrence limitée, polie)
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def _worker(url: str):
            async with sem:
                try:
                    bien = await _parse_detail(client, url)
                except Exception as e:
                    print(f"[LuthierNotaires36] Erreur détail {url}: {e}")
                    return None
                await asyncio.sleep(0.3)
                return bien

        biens = await asyncio.gather(*[_worker(u) for u in candidates])

        seen: set[str] = set()
        for bien in biens:
            if not bien:
                continue
            # Post-filtre dept STRICT (0 fuite)
            if not bien["code_postal"] or bien["code_postal"][:2] not in departements:
                continue
            if bien["id_annonce"] in seen:
                continue

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            seen.add(bien["id_annonce"])
            results.append(bien)

    # Comptage par département pour le log
    by_dept: dict[str, int] = {}
    for b in results:
        d = b["code_postal"][:2]
        by_dept[d] = by_dept.get(d, 0) + 1
    print(f"[LuthierNotaires36] {len(results)} biens retenus — par dept: {by_dept}")
    return results


async def _list_communes(client: httpx.AsyncClient) -> list[str]:
    r = await client.get(INDEX_URL)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    urls: set[str] = set()
    for a in soup.select('a[href*="/recherche/"]'):
        href = a.get("href", "")
        # On ne garde que les pages communales /recherche/{insee}/{ville-cp}.html
        if re.search(r"/recherche/\d+/", href):
            urls.add(_abs(href))
    return sorted(urls)


async def _list_details(client: httpx.AsyncClient, commune_url: str) -> set[str]:
    r = await client.get(commune_url)
    if r.status_code != 200:
        return set()
    soup = BeautifulSoup(r.text, "html.parser")
    out: set[str] = set()
    for a in soup.select('a[href*="/annonce/"]'):
        href = a.get("href", "")
        if "/annonce/" in href:
            out.add(_abs(href))
    return out


async def _parse_detail(client: httpx.AsyncClient, url: str) -> dict | None:
    r = await client.get(url)
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    # Localisation / surface / pièces : depuis le slug (fiable)
    ville = _ville_from_slug(url)
    code_postal = _cp_from_slug(url) or ""
    surface = _surface_from_slug(url)
    pieces = _pieces_from_slug(url)
    type_seg = _type_from_slug(url)
    type_bien = type_seg.replace("-a-vendre", "").replace("-", " ").strip() or "maison"

    # Titre : <title> ou h1
    titre = ""
    if soup.title and soup.title.string:
        titre = soup.title.string.strip()
    if not titre:
        h1 = soup.select_one("h1")
        titre = h1.get_text(" ", strip=True) if h1 else ""
    titre = re.sub(r"\s+", " ", titre)

    # CP / ville en secours via le <title> si absent du slug
    if not code_postal:
        m = re.search(r"\b(\d{5})\b", titre)
        if m:
            code_postal = m.group(1)

    # Prix : .product-price (ou tout texte "NNN NNN €")
    prix = _extract_prix(soup, r.text)

    # DPE (lettre A–G ; on ignore le GES)
    dpe = _extract_dpe(soup, r.text)

    # Description : meta description / og:description
    description = ""
    md = soup.select_one('meta[name="description"]') or soup.select_one(
        'meta[property="og:description"]'
    )
    if md and md.get("content"):
        description = md["content"].strip()

    # Photos
    photos: list[str] = []
    for img in soup.select("img"):
        src = img.get("data-src") or img.get("src") or ""
        if "photos.notariat.services/photos/" in src:
            if src.startswith("//"):
                src = "https:" + src
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    # id_annonce : référence dans l'URL (…/annonce/{ref}/…)
    m_ref = re.search(r"/annonce/([^/]+)/", url)
    id_annonce = m_ref.group(1) if m_ref else url

    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    return {
        "source": "notaires_luthier_36",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "SELARL Luthier & Penin-Maillet (notaires, Buzançais)",
    }


# ── Helpers slug ──────────────────────────────────────────────────────────────

def _slug(url: str) -> str:
    """Renvoie le dernier segment de l'URL (le slug), sans .html."""
    seg = url.rstrip("/").split("/")[-1]
    return seg[:-5] if seg.endswith(".html") else seg


def _cp_from_slug(url: str) -> str | None:
    """maison-a-vendre-saint-genou-36500-139m2-4pieces → 36500"""
    m = re.search(r"-(\d{5})(?:-|$)", _slug(url))
    return m.group(1) if m else None


def _surface_from_slug(url: str) -> float | None:
    m = re.search(r"-(\d+)m2(?:-|$)", _slug(url))
    if m:
        try:
            v = float(m.group(1))
            if 8 <= v <= 5000:
                return v
        except ValueError:
            pass
    return None


def _pieces_from_slug(url: str) -> int | None:
    m = re.search(r"-(\d+)pieces?(?:-|$)", _slug(url))
    return int(m.group(1)) if m else None


def _ville_from_slug(url: str) -> str:
    """maison-a-vendre-saint-genou-36500-139m2-4pieces → Saint-Genou"""
    slug = _slug(url)
    # tout ce qui précède '-{cp}' après avoir retiré le préfixe type/à-vendre
    m = re.search(r"-(\d{5})(?:-|$)", slug)
    head = slug[: m.start()] if m else slug
    head = re.sub(r"^.*?-a-vendre-", "", head)  # retire 'maison-a-vendre-' etc.
    if head == slug:  # pas de 'a-vendre' → retire juste le 1er mot de type
        head = re.sub(r"^[a-z]+-", "", head, count=1)
    ville = head.replace("-", " ").strip()
    return ville.title()


def _type_from_slug(url: str) -> str:
    """Renvoie le préfixe type du slug (avant '-a-vendre')."""
    slug = _slug(url)
    m = re.match(r"^(.*?)-a-vendre", slug)
    if m:
        return m.group(1)
    # sinon, 1er mot
    return slug.split("-")[0]


# ── Helpers extraction ────────────────────────────────────────────────────────

def _extract_prix(soup: BeautifulSoup, raw: str) -> float | None:
    el = soup.select_one(".product-price") or soup.select_one('[class*="price"]')
    text = el.get_text(" ", strip=True) if el else ""
    prix = _first_price(text)
    if prix:
        return prix
    # secours : titre "… à NNN NNN euros"
    m = re.search(r"(\d[\d\s\xa0]{3,})\s*euros", raw, re.IGNORECASE)
    if m:
        return _to_float(m.group(1))
    return _first_price(raw)


def _first_price(text: str) -> float | None:
    """Premier montant 'NNN NNN €' >= 1000 (évite les honoraires faibles)."""
    for m in re.finditer(r"([\d][\d\s\xa0]{2,})\s*€", text):
        v = _to_float(m.group(1))
        if v and v >= 1000:
            return v
    return None


def _to_float(s: str) -> float | None:
    cleaned = re.sub(r"[\s\xa0]", "", s)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _extract_dpe(soup: BeautifulSoup, raw: str) -> str | None:
    """DPE = lettre de classe énergie (consommation), pas le GES."""
    # cherche un bloc dpe avec une valeur numérique de conso + déduire la classe
    el = soup.select_one('[class*="dpe"]')
    block = el.get_text(" ", strip=True) if el else ""
    # Cherche une valeur de conso en kWh puis mappe en classe
    m = re.search(r"([0-9]{1,3}(?:[.,][0-9])?)", block)
    if m:
        try:
            val = float(m.group(1).replace(",", "."))
            return _dpe_class(val)
        except ValueError:
            pass
    return None


def _dpe_class(conso: float) -> str:
    if conso <= 70:
        return "A"
    if conso <= 110:
        return "B"
    if conso <= 180:
        return "C"
    if conso <= 250:
        return "D"
    if conso <= 330:
        return "E"
    if conso <= 420:
        return "F"
    return "G"


def _abs(href: str) -> str:
    return href if href.startswith("http") else BASE_URL + href


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
    print(f"\nTotal Luthier Notaires 36: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — DPE {b.get('dpe') or '?'}"
            f" — {b['type_bien']} — {b['ville']}"
        )

"""scrapers/etreproprio.py — EtreProprio (agrégateur national d'annonces d'agences)

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /maison-a-vendre/{NN}      (ex: /maison-a-vendre/72)
              → filtre département CÔTÉ SERVEUR par numéro de dept (tout dept FR).
              La liste rend 60 cartes SSR ; pas de pagination httpx fiable
              (chargement AJAX/scroll) → on exploite les 60 cartes de tête.

Cartes liste : a.ep-card-cla-a (href = page détail)
  - URL détail : href  → /immobilier-{ID}-vente-maison-...
  - ID annonce : segment immobilier-{ID}-
  - Titre      : .ep-title   →  "Maison 75 m² à Conlie"  (surface dans le titre)
  - Ville      : .ep-city
  - Prix       : .ep-price   →  "94 990 €"
  - Photo      : .ep-img img[src]
  - Agence     : .ep-rea img[title]
  - Desc       : .ep-desc (tronquée)

⚠️ La carte liste NE contient PAS le code postal. Pour garantir 0 fuite
hors-département (le filtre serveur /NN n'est pas vérifiable autrement), on
récupère la page détail de chaque bien : elle embarque un blob JS contenant
"postalCode":"NNNNN","departmentCode":"NN" + price/houseArea/terrainArea/roomNb/
dpeGlobalLetter. On post-filtre STRICT sur code_postal[:2] == dept.

Pour rester poli et borné, on limite à MAX_CARDS biens/dept et on récupère les
pages détail avec une concurrence plafonnée.

Couverture : agrégateur national (mandataires + agences), gros stock par dept
cible (3000+ maisons en Sarthe). On ne garde que les maisons (segment d'URL).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.etreproprio.com"
MAX_CARDS = 60          # cartes SSR rendues par page liste (pas de pagination httpx)
DETAIL_CONCURRENCY = 6  # requêtes détail simultanées (politesse)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
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
        for dept in departements:
            try:
                biens = await _scrape_dept(
                    client, dept, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[EtreProprio] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[EtreProprio] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    url = f"{BASE_URL}/maison-a-vendre/{dept}"
    r = await client.get(url)
    if r.status_code != 200:
        print(f"[EtreProprio] Dept {dept}: HTTP {r.status_code}")
        return []

    cards = BeautifulSoup(r.text, "html.parser").select("a.ep-card-cla-a")[:MAX_CARDS]
    if not cards:
        return []

    # Pré-parse des cartes (titre/ville/prix/photo/url/id) ; pré-filtre prix
    # liste pour éviter de récupérer des pages détail inutiles.
    stubs: list[dict] = []
    seen_ids: set[str] = set()
    for card in cards:
        stub = _parse_card(card)
        if not stub:
            continue
        if stub["id_annonce"] in seen_ids:
            continue
        p = stub.get("prix") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        seen_ids.add(stub["id_annonce"])
        stubs.append(stub)

    # Enrichissement via pages détail (CP/dept/terrain/pièces/dpe) avec concurrence bornée
    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def enrich(stub: dict) -> dict | None:
        async with sem:
            try:
                rd = await client.get(stub["url"])
                await asyncio.sleep(0.2)
            except Exception:
                return None
            if rd.status_code != 200:
                return None
            return _merge_detail(stub, rd.text, dept)

    enriched = await asyncio.gather(*(enrich(s) for s in stubs))

    biens: list[dict] = []
    for bien in enriched:
        if not bien:
            continue
        # Post-filtre dept STRICT : 0 fuite hors-département
        if not bien["code_postal"] or bien["code_postal"][:2] != dept:
            continue
        s = bien.get("surface") or 0
        if surface_min and s and s < surface_min:
            continue
        biens.append(bien)

    return biens


def _parse_card(card) -> dict | None:
    href = card.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    m = re.search(r"immobilier-(\d+)-", href)
    id_annonce = m.group(1) if m else url

    title_el = card.select_one(".ep-title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    city_el = card.select_one(".ep-city")
    ville = city_el.get_text(" ", strip=True) if city_el else ""

    price_el = card.select_one(".ep-price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    desc_el = card.select_one(".ep-desc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    rea_el = card.select_one(".ep-rea img")
    agence = (rea_el.get("title") or rea_el.get("alt")) if rea_el else None

    photos = []
    img = card.select_one(".ep-img img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)

    # Surface depuis le titre ("Maison 75 m² à ...")
    surface = _parse_surface_title(titre)

    return {
        "source": "etreproprio",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": description[:1200],
        "departement": None,
        "ville": _titlecase(ville)[:80],
        "code_postal": "",
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": agence,
    }


def _merge_detail(stub: dict, html: str, dept: str) -> dict:
    """Complète le stub avec le blob JS de la page détail (CP/dept/terrain/pièces/dpe)."""
    bien = dict(stub)

    # postalCode + departmentCode appariés (évite l'adresse du siège dans le JSON-LD Organization)
    m_cp = re.search(r'"postalCode":"(\d{5})","departmentCode":"(\d{2,3})"', html)
    if m_cp:
        bien["code_postal"] = m_cp.group(1)
        bien["departement"] = m_cp.group(2)
    else:
        # secours : departmentCode seul (n'apparaît que dans le blob annonce)
        m_d = re.search(r'"departmentCode":"(\d{2,3})"', html)
        if m_d:
            bien["departement"] = m_d.group(1)

    if bien.get("departement") is None:
        bien["departement"] = dept

    # Enrichissements numériques
    ha = _blob_num(html, "houseArea")
    if ha:
        bien["surface"] = ha
    ta = _blob_num(html, "terrainArea")
    if ta:
        bien["surface_terrain"] = ta
    rn = _blob_int(html, "roomNb")
    if rn:
        bien["pieces"] = rn
    pr = _blob_int(html, "price")
    if pr and not bien.get("prix"):
        bien["prix"] = float(pr)

    m_dpe = re.search(r'"dpeGlobalLetter":"([A-Ga-g])"', html)
    if m_dpe:
        bien["dpe"] = m_dpe.group(1).upper()

    return bien


# ── Helpers ──────────────────────────────────────────────────────────────────

def _blob_num(html: str, key: str) -> float | None:
    m = re.search(r'"' + key + r'":"?([\d.]+)"?', html)
    if m:
        try:
            v = float(m.group(1))
            return v if v > 0 else None
        except ValueError:
            return None
    return None


def _blob_int(html: str, key: str) -> int | None:
    m = re.search(r'"' + key + r'":"?(\d+)"?', html)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface_title(text: str) -> float | None:
    """'Maison 75 m² à Conlie' → 75.0"""
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0]*)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 3000:
                return f
        except ValueError:
            pass
    return None


def _titlecase(s: str) -> str:
    """'YVRE-LE-POLIN' → 'Yvre-Le-Polin'"""
    if not s:
        return s
    return "-".join(p.capitalize() for p in s.lower().split("-"))


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
    print(f"\nTotal EtreProprio: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['pieces'] or '?'}p — dpe {b['dpe'] or '?'} — {b['ville']}"
        )

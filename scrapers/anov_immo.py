"""scrapers/anov_immo.py — Anov'Immo (réseau de mandataires / CMS Adaptimmo)

Méthode : scrape_simple (httpx) — SSR HTML (CMS Adaptimmo, gabarit Tailwind,
même famille que declic_immo mais template plus récent).

Stratégie de découverte :
  - Le listing paginé (/fr/maison-villa-propriete-p-r70-1-{N}.html) REDIRIGE vers
    l'accueil (URL expirée/inexploitable) et l'accueil ne montre que ~10 biens
    "vedette". Le vrai inventaire est dans le SITEMAP XML
    (https://www.anov.immo/sitemap.xml) : ~520 fiches /vente/{idville}-{ville}/{type}/{ref}-{slug}.
  - On y filtre par TYPE (segment d'URL : maison/propriete/villa…) pour ne garder
    que les maisons/propriétés (~365), puis on enrichit chaque fiche.

Pas de filtre département serveur ni de code postal dans la liste/sitemap (le slug
ne contient que l'idville interne + le nom de ville, jamais le CP). On POST-FILTRE
donc sur le code postal extrait de la FICHE DÉTAIL.

Fiche détail (un seul GET par bien, tout y est) :
  - JSON-LD Product .name :
      "Maison 4pièce(s) 3chambre(s) 118 m² Château-Bernard (38650)"
      → type, pièces, chambres, surface habitable, ville, CODE POSTAL.
    .image[] → galerie photos ; .offers.price → prix ; .description / og:description.
  - .properties-detail__price        → "229 500 €" (prix de repli si JSON-LD vide)
  - .title-v1__part1                  → "Ville (CODEPOSTAL)" (repli localisation)
  - .bubble_dpe_{x}.bubble--active    → lettre DPE (a..g)

Filtre département : double barrière — code_postal[:2] de la fiche DOIT être dans
les départements ciblés. Aucune fuite possible (on jette tout CP hors-zone).

Couverture : réseau de mandataires (~43 conseillers, 17 départements), inventaire
national dispersé (Charente, Vienne, Vendée, Isère, Aveyron…). Stock variable sur
les 11 départements cibles du projet.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.anov.immo"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
DETAIL_CONCURRENCY = 6
PHOTOS_MAX = 12


# Types (segment d'URL /vente/{ville}/{TYPE}/…) à conserver : maisons / propriétés…
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|mas|chalet|longere|longère|manoir|"
    r"chateau|château|moulin|demeure|domaine|grange|gite|gîte|pavillon|haras",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|duplex|triplex|terrain|garage|immeuble|autre|"
    r"rez-de-jardin|local|commerce|parking|bureau|fonds|cave",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        urls = await _detail_urls(client)
        print(f"[AnovImmo] {len(urls)} fiches maison/propriété dans le sitemap")

        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def enrich(u):
            async with sem:
                b = await _parse_detail(client, u)
                await asyncio.sleep(0.15)
                return b

        biens = await asyncio.gather(*(enrich(u) for u in urls))

    results: list[dict] = []
    seen: set[str] = set()
    for bien in biens:
        if not bien:
            continue

        cp = bien.get("code_postal") or ""
        dept = cp[:2] if len(cp) >= 2 else ""
        # Filtre département STRICT (post-filtre CP) — 0 fuite
        if not dept or (departements and dept not in departements):
            continue
        bien["departement"] = dept

        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        aid = bien.get("id_annonce") or bien.get("url")
        if aid in seen:
            continue
        seen.add(aid)
        results.append(bien)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[AnovImmo] Dept {dept}: {n} annonces")

    return results


async def _detail_urls(client: httpx.AsyncClient) -> list[str]:
    """Extrait du sitemap les fiches détail /vente/.../{type}/{ref}-{slug},
    filtrées sur les types maison/propriété."""
    r = None
    for attempt in range(3):
        try:
            r = await client.get(SITEMAP_URL)
            r.raise_for_status()
            break
        except Exception as e:
            if attempt == 2:
                print(f"[AnovImmo] Sitemap inaccessible: {e}")
                return []
            await asyncio.sleep(1.5 * (attempt + 1))
    if r is None:
        return []

    locs = re.findall(r"<loc>(.*?)</loc>", r.text)
    urls: list[str] = []
    seen: set[str] = set()
    for loc in locs:
        if "/vente/" not in loc:
            continue
        parts = loc.split("/vente/", 1)[1].split("/")
        if len(parts) < 3:
            continue  # pas une fiche détail (ex: /vente/{ville}/{page})
        type_seg = parts[1]
        if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
            continue
        if not _KEEP_TYPE.search(type_seg):
            continue
        if loc in seen:
            continue
        seen.add(loc)
        urls.append(loc)
    return urls


async def _parse_detail(client: httpx.AsyncClient, url: str) -> dict | None:
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.content, "html.parser")
    except Exception:
        return None

    # Référence = id produit (segment final de l'URL : /{ref}-{slug})
    parts = [p for p in url.split("/vente/", 1)[1].split("/") if p]
    type_seg = parts[1] if len(parts) > 1 else ""
    ref = ""
    if len(parts) >= 3:
        m = re.match(r"^(\d+)-", parts[2])
        if m:
            ref = m.group(1)
    id_annonce = ref or url

    # ── JSON-LD Product : source principale (name encode tout) ──
    product = _find_product_jsonld(soup)
    ville = code_postal = ""
    surface = pieces = chambres = None
    prix = None
    type_bien = re.sub(r"^\d+-", "", type_seg).replace("-", " ").strip() or "maison"
    description = ""
    photos: list[str] = []

    if product:
        name = product.get("name") or ""
        ville, code_postal = _loc_from_name(name)
        pieces = _int_after(name, r"(\d+)\s*pi[èe]ce")
        chambres = _int_after(name, r"(\d+)\s*chambre")
        surface = _surface_from_name(name)
        type_bien = _type_from_name(name) or type_bien
        offers = product.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        prix = _to_num(offers.get("price"))
        description = (product.get("description") or "").strip()
        for src in product.get("image", []) or []:
            if isinstance(src, str) and src:
                if src.startswith("//"):
                    src = "https:" + src
                if src.startswith("http"):
                    photos.append(src)

    # ── Replis HTML si JSON-LD incomplet ──
    if not code_postal:
        loc_el = soup.select_one(".title-v1__part1")
        if loc_el:
            ville, code_postal = _parse_loc(loc_el.get_text(" ", strip=True))

    if prix is None:
        price_el = soup.select_one(".properties-detail__price")
        if price_el:
            prix = _to_num(price_el.get_text(" ", strip=True))

    # Description plus complète via og:description si JSON-LD vide
    if not description:
        og = soup.select_one('meta[property="og:description"]')
        if og and og.get("content"):
            description = og["content"].strip()

    # DPE : bulle active bubble_dpe_{x} + bubble--active
    dpe = _dpe_active(soup)

    # Terrain : tenté dans la description ("3253 m² de terrain", "terrain de 940 m²")
    surface_terrain = _terrain_from_text(description) or _terrain_from_text(
        (product or {}).get("name", "")
    )

    if not photos:
        for img in soup.select("img[src*='staticlbi'], img[data-src*='staticlbi']"):
            src = img.get("src") or img.get("data-src") or ""
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http"):
                photos.append(src.split("?")[0])
    # dédup en conservant l'ordre
    uniq: list[str] = []
    for s in photos:
        if s not in uniq:
            uniq.append(s)
    photos = uniq[:PHOTOS_MAX]

    titre = ""
    if product and product.get("name"):
        titre = product["name"]
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    return {
        "source": "anov_immo",
        "url": url if url.startswith("http") else BASE_URL + url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": (type_bien or "maison").lower(),
        "description": (description or "")[:1200] or None,
        "departement": code_postal[:2] if code_postal else "",
        "ville": (ville.title()[:80] if ville else None),
        "code_postal": code_postal or None,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Anov'Immo",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _find_product_jsonld(soup) -> dict | None:
    for s in soup.select('script[type="application/ld+json"]'):
        raw = s.string or s.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for it in data if isinstance(data, list) else [data]:
            if isinstance(it, dict) and it.get("@type") == "Product":
                return it
    return None


def _loc_from_name(name: str) -> tuple[str, str]:
    """'Maison 4pièce(s) … 118 m² Château-Bernard (38650)' → ('Château-Bernard','38650')"""
    cp = ""
    m = re.search(r"\((\d{5})\)", name)
    if m:
        cp = m.group(1)
    ville = ""
    # ville = tout entre la dernière surface 'm²' et la parenthèse du CP
    m2 = re.search(r"m²\s*(.+?)\s*\(\d{5}\)", name)
    if m2:
        ville = m2.group(1).strip()
    elif cp:
        ville = re.sub(r"\s*\(\d{5}\)\s*$", "", name).split("m²")[-1].strip()
    return ville, cp


def _parse_loc(text: str) -> tuple[str, str]:
    cp = ""
    m = re.search(r"\((\d{5})\)", text)
    if m:
        cp = m.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _type_from_name(name: str) -> str:
    m = re.match(r"^([A-Za-zÀ-ÿ' \-]+?)\s+\d", name)
    if m:
        return m.group(1).strip().lower()
    return ""


def _int_after(text: str, pattern: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _surface_from_name(name: str) -> float | None:
    m = re.search(r"([\d\s\xa0]+(?:[.,]\d+)?)\s*m²", name)
    if m:
        return _to_num(m.group(1))
    return None


def _terrain_from_text(text: str) -> float | None:
    if not text:
        return None
    m = re.search(
        r"(?:terrain[^0-9]{0,15}|)([\d\s\xa0]{2,})\s*m²?\s*de\s+terrain", text, re.I
    )
    if not m:
        m = re.search(r"terrain\s+(?:de\s+)?([\d\s\xa0]{2,})\s*m²", text, re.I)
    if m:
        val = _to_num(m.group(1))
        if val and 30 <= val <= 5_000_000:
            return val
    return None


def _dpe_active(soup) -> str | None:
    for b in soup.select('[class*="bubble_dpe_"]'):
        classes = b.get("class", [])
        if any("--active" in c for c in classes):
            for c in classes:
                m = re.match(r"bubble_dpe_([a-g])$", c)
                if m:
                    return m.group(1).upper()
    return None


def _to_num(value) -> float | None:
    if value is None:
        return None
    text = str(value)
    cleaned = text.replace("\xa0", " ")
    cleaned = re.sub(r"[^\d,\. ]", "", cleaned).replace(" ", "")
    cleaned = cleaned.replace(",", ".")
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


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
    print(f"\nTotal Anov'Immo (depts cibles): {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:15]:
        print(
            f"  [{b.get('code_postal')}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — DPE {b.get('dpe') or '?'}"
            f" — {b['type_bien']} — {b['ville']}"
        )

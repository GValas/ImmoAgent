"""scrapers/immobilier_bourgogne.py — Portail Immobilier Bourgogne

Méthode : scrape_simple (httpx) — SSR HTML (CMS "GLI portail régional" / pagesimmoweb,
          encodage ISO-8859-1).

Portail RÉGIONAL Bourgogne. Listing maisons à vendre :
    https://www.immobilier-bourgogne.net/type_bien/4-40/a-vendre.html
    pagination : /type_bien/4-40_{N}/portail-immobilier-maisons-a-vendre-page-{N}.html
                 (lien <link rel="next"> présent tant qu'il reste une page)

Cartes liste : div#listing_bien > div.col-md-4
  - URL fiche : h5 a[href]  → ../fiches/{cat}-{id}/slug.html
  - Titre     : h5 a (texte) → "VILLE - Maison NN m2 - N CH. 215 000 €"
  - Prix      : span.prix
  - Photos    : div.item[style="background: url(...)"]
  - PAS DE CODE POSTAL SUR LA CARTE → seule la ville (en majuscules) figure.

Le **code postal** (donc le département) n'apparaît QUE sur la fiche détail, dans
un bloc clé/valeur fiable :
    <div class="col-sm-6">Code postal</div><div class="col-sm-6"><b>21370</b></div>
On VISITE donc chaque fiche pour récupérer CP/ville/surface/terrain/pièces/DPE, puis
on POST-FILTRE par code_postal[:2] (0 fuite garantie).

Couverture réelle (test 2026-05-30) : inventaire maisons = ~22 biens, TOUS en
Côte-d'Or (21) — agence unique CADR'IMMO (Dijon). 0 bien dans les départements
cibles (58/89 inclus). → scraper fonctionnel mais sans stock pour notre zone
(actif: false). À réévaluer si l'inventaire régional s'élargit hors 21.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.immobilier-bourgogne.net"
LISTING_FIRST = f"{BASE_URL}/type_bien/4-40/a-vendre.html"
MAX_PAGES = 10
MAX_FICHE_CONCURRENCY = 5
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Mots-clés type → on ne garde que maisons / propriétés / demeures
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas\b|corps de ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|box",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
    ) as client:
        fiche_urls = await _collect_fiche_urls(client)
        if not fiche_urls:
            print("[ImmoBourgogne] Aucune fiche trouvée (listing vide ?)")
            return []

        sem = asyncio.Semaphore(MAX_FICHE_CONCURRENCY)

        async def _one(url: str) -> dict | None:
            async with sem:
                return await _parse_fiche(client, url)

        parsed = await asyncio.gather(*[_one(u) for u in fiche_urls])

    results: list[dict] = []
    seen: set[str] = set()
    for bien in parsed:
        if not bien:
            continue

        # POST-FILTRE département via code_postal[:2]
        cp = bien.get("code_postal") or ""
        dept = cp[:2] if len(cp) >= 2 else ""
        if departements and dept not in departements:
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
    if by_dept:
        for dept, n in sorted(by_dept.items()):
            print(f"[ImmoBourgogne] Dept {dept}: {n} annonces")
    else:
        print(
            f"[ImmoBourgogne] 0 annonce dans les depts cibles "
            f"({len(fiche_urls)} maisons scannées, toutes hors-zone)"
        )

    return results


async def _collect_fiche_urls(client: httpx.AsyncClient) -> list[str]:
    """Parcourt les pages de listing 'maisons à vendre' et collecte les URLs fiches."""
    urls: list[str] = []
    seen: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = LISTING_FIRST
        else:
            url = (
                f"{BASE_URL}/type_bien/4-40_{page}/"
                f"portail-immobilier-maisons-a-vendre-page-{page}.html"
            )
        r = await _get(client, url)
        if r is None or r.status_code != 200:
            break

        soup = BeautifulSoup(r.content.decode("iso-8859-1", "replace"), "html.parser")
        cards = soup.select("div#listing_bien > div.col-md-4")
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            a = card.select_one("h5 a[href]")
            if not a:
                continue
            href = a["href"]
            full = href if href.startswith("http") else BASE_URL + "/" + href.lstrip("./")
            if "/fiches/" not in full:
                continue
            if full in seen:
                continue
            seen.add(full)
            urls.append(full)
            new_on_page += 1

        # Dernière page : plus de lien rel=next, ou rien de neuf
        if not soup.select_one("link[rel=next]") or new_on_page == 0:
            break

        await asyncio.sleep(0.4)

    return urls


async def _parse_fiche(client: httpx.AsyncClient, url: str) -> dict | None:
    r = await _get(client, url)
    if r is None or r.status_code != 200:
        return None
    html = r.content.decode("iso-8859-1", "replace")

    kv = _key_values(html)

    type_bien_raw = kv.get("Type de bien", "")
    # Filtre type : on exclut explicitement appartement/terrain/etc.
    if _EXCLUDE_TYPE.search(type_bien_raw) and not _KEEP_TYPE.search(type_bien_raw):
        return None
    if type_bien_raw and not _KEEP_TYPE.search(type_bien_raw):
        # type connu mais non désiré (ex: "Appartement")
        return None
    type_bien = (type_bien_raw or "maison").strip().lower()

    code_postal = kv.get("Code postal", "")
    code_postal = re.sub(r"\D", "", code_postal)[:5]
    ville = kv.get("Ville", "").strip().title()

    prix = _num(kv.get("Prix", ""))
    surface = _num(kv.get("Surface", ""))
    surface_terrain = _num(kv.get("Surface terrain", ""))
    pieces = _int(kv.get("Nombre pièces", "") or kv.get("Nombre pieces", ""))
    chambres = _int(kv.get("Chambres", ""))
    dpe = _dpe(kv.get("Diagnostic Perf. Numérique", ""))

    # id annonce : depuis l'URL  /fiches/{cat}_{id}/...
    m_id = re.search(r"/fiches/[^/]*?_(\d+)/", url)
    id_annonce = m_id.group(1) if m_id else url

    # Titre : balise <title> de la fiche, sinon reconstruit
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.find("title")
    titre = title_el.get_text(strip=True) if title_el else ""
    if not titre or len(titre) < 4:
        titre = f"{type_bien.title()} {ville}".strip()

    # Description : meta description
    description = ""
    md = soup.find("meta", attrs={"name": "description"})
    if md and md.get("content"):
        description = md["content"].strip()

    # Photos : pattern d'URLs d'images dans la fiche
    photos = _photos(html)

    return {
        "source": "immobilier_bourgogne",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "dpe": dpe,
        "photos": photos,
        "agence": "Portail Immobilier Bourgogne",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    for _ in range(3):
        try:
            return await client.get(url)
        except Exception:
            await asyncio.sleep(0.8)
    return None


def _key_values(html: str) -> dict[str, str]:
    """Extrait les paires <div class=col-sm-6>Label</div><div ...><b>Valeur</b>."""
    kv: dict[str, str] = {}
    for m in re.finditer(
        r'<div class="col-sm-6">([^<]+)</div>\s*'
        r'<div class="col-sm-6"><b>(.*?)</b>',
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        label = re.sub(r"\s+", " ", m.group(1)).strip()
        val = re.sub(r"<[^>]*>", " ", m.group(2))
        val = re.sub(r"\s+", " ", val).strip()
        if label and label not in kv:
            kv[label] = val
    return kv


def _num(text: str) -> float | None:
    """'215000 EUR' / '79 m2' / '946 m2' / '139,86 m2' → float.

    Coupe d'abord à l'unité (m2/m²/EUR/€) pour éviter que le '2' de 'm2'
    ne soit happé dans le nombre.
    """
    if not text:
        return None
    # On ne garde que la partie numérique avant toute unité.
    m = re.search(r"[\d][\d\s\xa0.,]*", text)
    if not m:
        return None
    cleaned = m.group(0).replace("\xa0", "").replace(" ", "").strip(".,")
    # Séparateur de milliers (point) vs décimal (virgule) : '139.862' rare ;
    # ici les surfaces utilisent ',' décimal et les prix sont des entiers.
    cleaned = cleaned.replace(",", ".")
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _int(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


def _dpe(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"\b([A-G])\b", text.upper())
    return m.group(1) if m else None


def _photos(html: str) -> list[str]:
    """Photos de la fiche : .../images/pr_p/.../{id}{lettre}.jpg
    (attributs src / data-src / data-lazy, ou background:url())."""
    photos: list[str] = []
    seen: set[str] = set()
    candidates = re.findall(
        r"(?:src|data-src|data-lazy|background:\s*url\()[=(]?\s*['\"]?"
        r"([^'\"() ]*?/images/pr_[pgm]/[^'\"() ]*?\.jpe?g)",
        html,
        re.IGNORECASE,
    )
    for src in candidates:
        src = src.strip()
        if not src or src.startswith("data:"):
            continue
        if src.startswith("../"):
            src = BASE_URL + "/" + src.lstrip("./")
        elif src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = BASE_URL + src
        elif not src.startswith("http"):
            src = BASE_URL + "/" + src
        if src not in seen:
            seen.add(src)
            photos.append(src)
        if len(photos) >= PHOTOS_PER_CARD:
            break
    return photos


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
    print(f"\nTotal Immobilier Bourgogne (depts cibles): {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

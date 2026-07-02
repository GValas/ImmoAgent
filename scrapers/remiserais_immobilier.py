"""scrapers/remiserais_immobilier.py — L'Immobilier par Rémi Serais

Méthode : scrape_simple (httpx) — SSR HTML (CMS Polaris/osCommerce, ISO-8859-1).
          Acteur historique de Normandie (Orne 61 + Calvados 14), transaction
          maisons / propriétés rurales.

Listing : /annonces/transaction/Vente.html?page={N}
  Cartes : div.product--transaction-vente
    - URL      : a.product-image[href]  (relatif ../fiches/...)
    - Titre    : .product-name > span (1er span)  /  Ville : dernier span
    - Prix     : .product-price  → "199 480 €"
    - Pièces   : .data-list__item--NbPiece .data-list__item--value
    - Surface  : .data-list__item--Surface .data-list__item--value (1ère = habitable)
    - Réf      : .data-list__item--products_model .data-list__item--value
    - Photos   : img.photo / img.photo-hidden

Filtre département : les cartes du listing N'EXPOSENT PAS le code postal
  (seul le nom de ville y figure). Le serveur ne fournit pas de filtre dept
  fiable sans session (advanced_search_result.php → 0 carte en GET simple).
  → Stratégie : on récupère le CODE POSTAL sur la PAGE DÉTAIL de chaque bien
    (table clé/valeur fiable : "Code postal" / "Ville"), puis POST-FILTRE STRICT
    code_postal[:2] in departements. La page détail enrichit aussi le bien
    (type, surface terrain, chambres, pièces, description). Concurrence limitée.

Couverture : site 100 % Normandie (Orne 61 + Calvados 14, parfois Manche 50 /
  Mayenne 53 limitrophes). Hors de cette zone (ex. 72/28/45/89) → 0 bien, ce qui
  est attendu : le post-filtre garantit 0 fuite hors-département.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.remiserais-immobilier.fr"
LISTING_URL = BASE_URL + "/annonces/transaction/Vente.html"
MAX_PAGES = 12
PHOTOS_PER_CARD = 10
DETAIL_CONCURRENCY = 6


# Types de bien à conserver : maisons / propriétés / fermes…
_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|mas|g[iî]te|pavillon|corps de ferme|maison de village|"
    r"maison de ville|fermette|chaumi[eè]re",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|bar|brasserie|hangar|entrep[oô]t|caf[eé]|restaurant|murs",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    if not departements:
        return results

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1) Collecter les stubs de cartes sur le listing national
        stubs = await _collect_stubs(client)
        print(f"[RemiSerais] {len(stubs)} cartes collectées sur le listing")

        # 2) Enrichir via page détail (code postal + champs) en parallèle limité
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def _enrich(stub):
            async with sem:
                return await _fetch_detail(client, stub)

        biens_raw = await asyncio.gather(*(_enrich(s) for s in stubs))

        # 3) Post-filtre STRICT par département + bornes
        seen: set[str] = set()
        for bien in biens_raw:
            if not bien:
                continue
            cp = bien.get("code_postal") or ""
            if not cp or cp[:2] not in departements:
                continue
            bien["departement"] = cp[:2]

            aid = bien["id_annonce"]
            if aid in seen:
                continue

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            seen.add(aid)
            results.append(bien)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print(f"[RemiSerais] {len(results)} biens retenus — par dept : {by_dept}")
    return results


async def _collect_stubs(client: httpx.AsyncClient) -> list[dict]:
    stubs: list[dict] = []
    seen_urls: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        url = f"{LISTING_URL}?page={page}"
        try:
            r = await client.get(url)
        except Exception as e:
            print(f"[RemiSerais] Erreur listing page {page}: {e}")
            break
        if r.status_code != 200:
            break
        html = r.content.decode("latin-1", errors="replace")
        cards = BeautifulSoup(html, "html.parser").select(
            ".product--transaction-vente"
        )
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            stub = _parse_card(card)
            if not stub:
                continue
            if stub["url"] in seen_urls:
                continue
            seen_urls.add(stub["url"])
            stubs.append(stub)
            new_on_page += 1

        if new_on_page == 0:
            break
        await asyncio.sleep(0.5)
    return stubs


def _parse_card(card) -> dict | None:
    link = card.select_one("a.product-image")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = _absurl(href)

    # Titre + ville (premier / dernier span du product-name)
    name_el = card.select_one(".product-name")
    titre = ""
    ville = ""
    if name_el:
        spans = [
            s.get_text(" ", strip=True)
            for s in name_el.find_all("span", recursive=False)
        ]
        spans = [s for s in spans if s]
        if spans:
            titre = spans[0]
            ville = spans[-1] if len(spans) > 1 else ""

    # Prix
    price_el = card.select_one(".product-price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Surface habitable (1ère valeur Surface) / pièces / réf
    surface = _first_dl_value(card, "Surface", to_float=True)
    pieces = _first_dl_value(card, "NbPiece", to_int=True)
    ref = _first_dl_value(card, "products_model")

    # id_annonce : id numérique dans le slug d'URL (../fiches/4-40-26_{ID}/...)
    m = re.search(r"_(\d+)/", href)
    id_num = m.group(1) if m else ""
    id_annonce = ref or id_num or url

    # Photos
    photos: list[str] = []
    for img in card.select("img.photo, img.photo-hidden"):
        src = img.get("src") or ""
        if src and not src.startswith("data:"):
            photos.append(_absurl(src))
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre,
        "ville": ville,
        "prix": prix,
        "surface": surface,
        "pieces": pieces,
        "photos": photos,
    }


async def _fetch_detail(client: httpx.AsyncClient, stub: dict) -> dict | None:
    """Récupère la page détail → code postal + champs, fusionne avec le stub."""
    try:
        r = await client.get(stub["url"])
    except Exception:
        return None
    if r.status_code != 200:
        return None
    html = r.content.decode("latin-1", errors="replace")

    fields = _parse_detail_fields(html)

    type_bien = (fields.get("Type de bien") or "").strip()
    # Filtre type : on écarte commerces / terrains / appartements
    type_for_check = type_bien or stub.get("titre", "")
    if _EXCLUDE_TYPE.search(type_for_check) and not _KEEP_TYPE.search(type_for_check):
        return None
    if not type_bien:
        type_bien = "maison"

    cp = _digits(fields.get("Code postal"))
    ville = (fields.get("Ville") or stub.get("ville") or "").strip()
    if ville:
        ville = ville.title()

    surface = _num(fields.get("Surface")) or stub.get("surface")
    surface_terrain = _num(fields.get("Surface terrain"))
    pieces = _int(fields.get("Nombre pièces")) or stub.get("pieces")
    chambres = _int(fields.get("Chambres"))
    prix = _num(fields.get("Prix")) or stub.get("prix")
    dpe = _parse_dpe(html)
    description = _parse_description(html)

    return {
        "source": "remiserais_immobilier",
        "url": stub["url"],
        "id_annonce": stub["id_annonce"],
        "titre": (stub.get("titre") or f"{type_bien} {ville}").strip()[:150],
        "type_bien": type_bien.lower(),
        "description": description[:1200],
        "departement": cp[:2] if cp else "",
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": stub.get("photos", []),
        "dpe": dpe,
        "agence": "L'Immobilier par Rémi Serais",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _absurl(href: str) -> str:
    if href.startswith("http"):
        return href
    href = href.lstrip(".")
    if not href.startswith("/"):
        href = "/" + href
    return BASE_URL + href


def _first_dl_value(card, key: str, to_int=False, to_float=False):
    el = card.select_one(
        f".data-list__item--{key} .data-list__item--value"
    )
    if not el:
        return None
    txt = el.get_text(" ", strip=True)
    if to_int:
        return _int(txt)
    if to_float:
        return _num(txt)
    return txt or None


def _parse_detail_fields(html: str) -> dict:
    """Table clé/valeur du détail : <div col-sm-6>LABEL</div><div col-sm-6>[<b>]VALUE."""
    fields: dict[str, str] = {}
    for lab, val in re.findall(
        r'<div class="col-sm-6">([^<]+)</div>'
        r'<div class="col-sm-6">(?:<b>)?([^<]*)',
        html,
    ):
        lab = lab.strip()
        val = val.strip()
        if lab and val and lab not in fields:
            fields[lab] = val
    return fields


def _parse_description(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for sel in (
        ".product-description",
        ".description",
        '[itemprop="description"]',
        "#product_description",
    ):
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(" ", strip=True)
            if len(txt) > 30:
                return txt
    # repli : meta description
    meta = soup.select_one('meta[name="description"]')
    return meta.get("content", "").strip() if meta else ""


def _parse_dpe(html: str) -> str | None:
    m = re.search(
        r"(?:DPE|Diagnostic de Performance|Consommation[^A-G]{0,40})"
        r"[^A-G]{0,30}\b([A-G])\b",
        html,
    )
    return m.group(1) if m else None


def _num(text) -> float | None:
    if text is None:
        return None
    s = re.sub(r"[^\d.,]", " ", str(text)).strip()
    s = s.split()[0] if s.split() else ""
    s = s.replace(",", ".")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _int(text) -> int | None:
    v = _num(text)
    return int(v) if v is not None else None


def _digits(text) -> str:
    if not text:
        return ""
    m = re.search(r"\d{5}", str(text))
    return m.group(0) if m else ""


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
    print(f"\nTotal Rémi Serais: {len(biens)} annonces")
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

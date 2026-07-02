"""scrapers/maisons_de_bretagne.py — Maisons de Bretagne (réseau local 3 agences, Bretagne Sud)

Méthode : scrape_simple (httpx) — SSR HTML (CMS Polaris/osCommerce, Apache, pas de Cloudflare).

URL pattern :
  Listing national paginé : /annonces/transaction/Vente.html?page={N}
    → ~11 biens/page, ~121 biens au total (11 pages).
  Le site est mono-zone (Finistère 29 + Morbihan 56) : PAS de slug département natif.
  → On scrape le listing national puis on POST-FILTRE strict sur code_postal[:2].

Cartes : div.item-product
  - URL    : a[href^='../fiches/'] (fiche détail) → /fiches/{code}_{id}/{slug}.html
  - id     : a.btn_buy_now[data-productid] ou .products-ref ("Ref. : 2343")
  - Titre  : .products-name
  - Texte  : .products-desc
  - Prix   : .products-price → "487 000 €"
  - Photo  : div.visuel-product img.photo[src]

Code postal & ville : ABSENTS de la carte de liste. La carte ne donne que le nom
  (qui contient la ville en texte libre). Le CP fiable est sur la PAGE DÉTAIL,
  dans des lignes <div class="row"><div class="col-sm-6">Code postal</div>
  <div class="col-sm-6"><b>29340</b></div></div>. On récupère donc le CP (+ ville,
  surface, pièces, terrain, chambres, dpe) sur la fiche détail de chaque bien, puis
  on filtre strictement sur le département → 0 fuite hors-zone.

Optimisation : comme la zone (29/56) ne recoupe aucun des départements cibles
  habituels (Val-de-Loire), on n'ouvre les fiches que si au moins un département
  29 ou 56 est demandé (sinon retour [] immédiat, aucune requête détail inutile).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.maisons-de-bretagne.com"
LISTING_URL = BASE_URL + "/annonces/transaction/Vente.html"
MAX_PAGES = 15
PHOTOS_PER_CARD = 1  # la carte de liste n'expose qu'une miniature ; détail non parsé pour galerie
DETAIL_CONCURRENCY = 6

# Départements réellement couverts par le réseau (Bretagne Sud).
COVERED_DEPTS = {"29", "56"}


_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    cibles = set(departements)
    # Zone mono-département : si aucun dept couvert n'est demandé, rien à scraper.
    if not (cibles & COVERED_DEPTS):
        print(
            f"[MaisonsBretagne] Aucun département couvert (29/56) dans la cible "
            f"{sorted(cibles)} → 0 bien."
        )
        return []

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        cards = await _collect_cards(client)
        print(f"[MaisonsBretagne] {len(cards)} cartes collectées (listing national)")

        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def enrich(card: dict) -> dict | None:
            async with sem:
                return await _enrich_and_filter(
                    client, card, cibles, prix_max, prix_min, surface_min
                )

        enriched = await asyncio.gather(*(enrich(c) for c in cards))

    for b in enriched:
        if b:
            results.append(b)

    par_dept: dict[str, int] = {}
    for b in results:
        par_dept[b["departement"]] = par_dept.get(b["departement"], 0) + 1
    print(f"[MaisonsBretagne] {len(results)} biens retenus — par dept : {par_dept}")
    return results


async def _collect_cards(client: httpx.AsyncClient) -> list[dict]:
    """Parcourt les pages du listing et renvoie les cartes brutes (sans CP)."""
    cards: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = f"{LISTING_URL}?page={page}"
        try:
            r = await client.get(url)
        except Exception as e:
            print(f"[MaisonsBretagne] Erreur page {page}: {e}")
            break
        if r.status_code != 200:
            break

        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select("div.item-product")
        if not items:
            break

        new_on_page = 0
        for item in items:
            card = _parse_card(item)
            if not card:
                continue
            if card["id_annonce"] in seen_ids:
                continue
            seen_ids.add(card["id_annonce"])
            cards.append(card)
            new_on_page += 1

        if new_on_page == 0:
            break
        await asyncio.sleep(0.5)

    return cards


def _parse_card(item) -> dict | None:
    link = item.select_one("div.visuel-product a[href]") or item.select_one(
        "a.products-link, div.products-link a[href], a[href*='/fiches/']"
    )
    href = link.get("href", "") if link else ""
    if not href or "/fiches/" not in href:
        # cherche n'importe quel lien fiche dans la carte
        for a in item.find_all("a", href=True):
            if "/fiches/" in a["href"]:
                href = a["href"]
                break
    if not href:
        return None
    url = _abs(href)

    name_el = item.select_one(".products-name")
    titre = name_el.get_text(" ", strip=True) if name_el else ""

    # type de bien : exclure appartement/terrain/etc.
    low = titre.lower()
    if _EXCLUDE_TYPE.search(low):
        return None
    type_bien = _type_from_title(titre)

    desc_el = item.select_one(".products-desc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    price_el = item.select_one(".products-price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # id_annonce
    id_annonce = ""
    fav = item.select_one("a[data-productid]")
    if fav and fav.get("data-productid"):
        id_annonce = fav["data-productid"].strip()
    if not id_annonce:
        ref_el = item.select_one(".products-ref")
        if ref_el:
            m = re.search(r"([\w-]+)\s*$", ref_el.get_text(strip=True))
            if m:
                id_annonce = m.group(1)
    if not id_annonce:
        m = re.search(r"_(\d+)/", href)
        id_annonce = m.group(1) if m else url

    photo_el = item.select_one("div.visuel-product img")
    photos = []
    if photo_el:
        src = photo_el.get("src") or photo_el.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(_abs(src))

    # surface / pièces extraits du titre (slug) quand présents
    surface = _parse_surface(titre)
    pieces = _parse_pieces(titre)

    return {
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "prix": prix,
        "surface": surface,
        "pieces": pieces,
        "photos": photos[:PHOTOS_PER_CARD],
    }


async def _enrich_and_filter(
    client: httpx.AsyncClient,
    card: dict,
    cibles: set[str],
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> dict | None:
    """Ouvre la fiche détail pour récupérer CP/ville et applique le post-filtre strict."""
    try:
        r = await client.get(card["url"])
    except Exception:
        return None
    if r.status_code != 200:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    fields = _detail_fields(soup)

    code_postal = fields.get("Code postal", "") or ""
    code_postal = re.sub(r"\D", "", code_postal)[:5]
    ville = (fields.get("Ville") or "").title()

    # Post-filtre département STRICT (0 fuite). Sans CP fiable → on écarte.
    if not code_postal or len(code_postal) != 5:
        return None
    dept = code_postal[:2]
    if dept not in cibles or dept not in COVERED_DEPTS:
        return None

    surface = (
        _parse_surface(fields.get("Surface", ""))
        or _parse_surface(fields.get("Surface habitable", ""))
        or card.get("surface")
    )
    pieces = (
        _parse_pieces(fields.get("Nombre pièces", ""))
        or _parse_pieces(fields.get("Nombre de pièces", ""))
        or card.get("pieces")
    )
    surface_terrain = _parse_surface(fields.get("Surface terrain", "")) or _parse_surface(
        fields.get("Surface du terrain", "")
    )
    chambres = _parse_pieces(fields.get("Chambres", "")) or _parse_pieces(
        fields.get("Nombre de chambres", "")
    )
    type_detail = fields.get("Type de bien", "")
    if type_detail and re.search(
        r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau",
        type_detail, re.IGNORECASE,
    ):
        return None
    dpe = (fields.get("DPE") or fields.get("Classe énergie") or None)
    if dpe:
        m = re.search(r"\b([A-G])\b", dpe.upper())
        dpe = m.group(1) if m else None

    prix = card.get("prix")
    # Bornes prix / surface (sans exclure si champ manquant)
    if prix_max and prix and prix > prix_max:
        return None
    if prix_min and prix and prix < prix_min:
        return None
    if surface_min and surface and surface < surface_min:
        return None

    return {
        "source": "maisons_de_bretagne",
        "url": card["url"],
        "id_annonce": card["id_annonce"],
        "titre": card["titre"],
        "type_bien": card["type_bien"],
        "description": card["description"],
        "departement": dept,
        "ville": (ville or _ville_from_title(card["titre"]))[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": card["photos"],
        "dpe": dpe,
        "agence": "Maisons de Bretagne",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

_WANTED_LABELS = {
    "Code postal", "Ville", "Type de bien",
    "Surface", "Surface habitable",
    "Surface terrain", "Surface du terrain",
    "Nombre pièces", "Nombre de pièces",
    "Chambres", "Nombre de chambres",
    "DPE", "Classe énergie",
}


def _detail_fields(soup) -> dict[str, str]:
    """Lit les lignes label/valeur de la fiche détail.

    Chaque caractéristique est une ligne
      <div class="row"><div class="col-sm-6">{label}</div>
      <div class="col-sm-6"><b>{valeur}</b></div></div>
    On repère le libellé par son texte, puis on lit le 2ᵉ col-sm-6 de la même ligne.
    """
    out: dict[str, str] = {}
    for label in _WANTED_LABELS:
        if label in out:
            continue
        node = soup.find(
            string=lambda t, lbl=label: t and t.strip() == lbl
        )
        if not node:
            continue
        col = node.parent
        sib = col.find_next_sibling("div") if col else None
        if sib is None and col is not None:
            # repli : 2 colonnes de la même row
            row = col.find_parent("div", class_="row")
            if row:
                cols = row.find_all("div", recursive=True)
                cols = [c for c in cols if "col-sm-6" in (c.get("class") or [])]
                if len(cols) == 2:
                    sib = cols[1]
        if sib is not None:
            val = sib.get_text(strip=True)
            if val:
                out[label] = val
    return out


def _abs(href: str) -> str:
    href = href.strip()
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    # Les fiches/images sont servies à la racine du site (/fiches/..., /office/...).
    # Les href de liste sont relatifs ("../fiches/...") : on normalise sur la racine
    # plutôt que de résoudre relativement au chemin du listing (qui dérive mal).
    for anchor in ("/fiches/", "/office/", "/images/", "/catalog/"):
        idx = href.find(anchor)
        if idx != -1:
            return BASE_URL + href[idx:]
    cleaned = re.sub(r"^(?:\.\./)+", "", href).lstrip("./")
    return f"{BASE_URL}/{cleaned}"


def _type_from_title(titre: str) -> str:
    low = titre.lower()
    for kw in (
        "longère", "longere", "manoir", "château", "chateau", "propriété",
        "propriete", "ferme", "moulin", "villa", "maison",
    ):
        if kw in low:
            return kw.replace("é", "e")
    return "maison"


def _ville_from_title(titre: str) -> str:
    # secours : "Maison Riec-sur-Belon 6 pièces ..." → "Riec-sur-Belon"
    m = re.match(r"^[A-Za-zÀ-ÿ']+\s+([A-Za-zÀ-ÿ'-]+(?:[ -][A-Za-zÀ-ÿ'-]+){0,3})", titre)
    return m.group(1).strip() if m else ""


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d].*$", "", cleaned)  # coupe au 1er non-chiffre (honoraires…)
    cleaned = re.sub(r"\D", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0]*)\s*m", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 1 <= f <= 100000:
                return f
        except ValueError:
            pass
    return None


def _parse_pieces(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d+)\s*pi[eè]ce", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"^\s*(\d+)\s*$", text)
    return int(m.group(1)) if m else None


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
    print(f"\nTotal Maisons de Bretagne: {len(biens)} annonces")
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

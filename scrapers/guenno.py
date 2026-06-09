"""scrapers/guenno.py — Guenno Immobilier (réseau ~10 agences Rennes / Ille-et-Vilaine)

Méthode : scrape_simple (httpx) — SSR HTML (moteur immo-facile).
Site mono-secteur : la quasi-totalité du stock est en Ille-et-Vilaine (35).
  → AUCUN bien dans la zone cible actuelle (72/28/45/89/49/37/36/18/58/41/53).
    Le scraper reste fonctionnel : il ne tournera que si le dept 35 est demandé,
    sinon il retourne [] immédiatement (court-circuit, 0 fuite par construction).

URL pattern liste : /biens/achat/maison?page=N   (SSR, 21 cartes/page)
  → pas de filtre département serveur fiable (slugs ville parfois trompeurs,
    ex. slug "guilvinec" pour un bien réellement à La Mézière 35690).
    DONC : on récupère le code postal AUTORITAIRE sur la page détail
    (<span itemprop="postalCode">35690</span>) et on post-filtre cp[:2]==dept.

Cartes liste : article[data-click] > .description
  - URL    : article[data-click] (= lien détail)  ou  a.link-block[href]
  - Prix   : .realty_price            →  "273 995 €"
  - Titre  : h2                        →  "Achat Maison Nord Saint-Martin"
  - Ville  : .infos span (1er, icône g_location)
  - Pièces : .infos span (icône g_room) → entier
  - Surface: .infos span (icône g_surface) → "90 M2"
  - Photo  : .photo[data-background-image]

Page détail (immo-facile) :
  - Code postal AUTORITAIRE : <span itemprop="postalCode">NNNNN</span>
  - Localité  : <span itemprop="addressLocality">…</span>
  - var realty = {…} : surface, surface_land, number_room, number_bedroom,
    energy_consumption (DPE), reference, description, price.

Type de bien : déduit du segment d'URL /biens/achat/{type}/… (on garde maisons /
               propriétés / longères, on exclut appartement/terrain/immeuble…).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import html
import json
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.guenno.com"
LISTING_PATH = "/biens/achat/maison"
MAX_PAGES = 8
PHOTOS_PER_CARD = 10
DETAIL_CONCURRENCY = 4

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Départements réellement couverts par Guenno (mono-secteur Ille-et-Vilaine).
# Si la zone cible ne contient pas 35 → search() retourne [] sans requête.
GUENNO_DEPTS: set[str] = {"35"}

_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"viager|investissement|bureaux",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Court-circuit : aucun département demandé n'est couvert par Guenno (35).
    cibles = [d for d in departements if d in GUENNO_DEPTS]
    if not cibles:
        print(
            f"[Guenno] Aucun département cible couvert (Guenno = 35 uniquement) ; "
            f"demandés={departements} → 0 annonce."
        )
        return []

    results: list[dict] = []
    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1) Collecte des cartes (toutes pages)
        cards = await _collect_cards(client)
        print(f"[Guenno] {len(cards)} cartes collectées sur la liste.")

        # 2) Résolution du code postal autoritaire + enrichissement (page détail)
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def enrich(card: dict) -> dict | None:
            async with sem:
                return await _build_bien(client, card, cibles)

        biens = await asyncio.gather(*(enrich(c) for c in cards))

        for bien in biens:
            if not bien:
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

    by_dept: dict[str, int] = {}
    for b in results:
        d = b["code_postal"][:2] if b["code_postal"] else "??"
        by_dept[d] = by_dept.get(d, 0) + 1
    print(f"[Guenno] {len(results)} annonces retenues par dept : {by_dept}")
    return results


async def _collect_cards(client: httpx.AsyncClient) -> list[dict]:
    cards: list[dict] = []
    seen_urls: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}{LISTING_PATH}?page={page}"
        try:
            r = await client.get(url)
        except Exception as e:
            print(f"[Guenno] Erreur liste page {page}: {e}")
            break
        if r.status_code != 200:
            break

        articles = BeautifulSoup(r.text, "html.parser").select("article[data-click]")
        if not articles:
            break

        new_on_page = 0
        for art in articles:
            c = _parse_card(art)
            if not c:
                continue
            if c["url"] in seen_urls:
                continue
            seen_urls.add(c["url"])
            cards.append(c)
            new_on_page += 1

        if new_on_page == 0:
            break
        await asyncio.sleep(0.5)
    return cards


def _parse_card(art) -> dict | None:
    href = art.get("data-click") or ""
    if not href:
        link = art.select_one("a.link-block")
        href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis l'URL : /biens/achat/{type}/{ville}/{Tn}/{id}
    parts = [p for p in url.replace(BASE_URL, "").split("/") if p]
    type_seg = parts[2] if len(parts) > 2 else ""
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg.replace("-", " ").strip() or "maison"

    # id_annonce = dernier segment numérique de l'URL
    id_annonce = parts[-1] if parts else url

    price_el = art.select_one(".realty_price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    title_el = art.select_one("h2")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    # .infos > span (location / room / surface) dans l'ordre
    infos = art.select(".infos span")
    ville = ""
    pieces = None
    surface = None
    for sp in infos:
        img = sp.find("img")
        icon = (img.get("src", "") if img else "").lower()
        txt = sp.get_text(" ", strip=True)
        if "g_location" in icon:
            ville = txt
        elif "g_room" in icon:
            m = re.search(r"\d+", txt)
            pieces = int(m.group()) if m else None
        elif "g_surface" in icon:
            m = re.search(r"([\d\s\xa0]+)", txt)
            if m:
                v = re.sub(r"[\s\xa0]", "", m.group(1))
                surface = float(v) if v else None

    photo_el = art.select_one(".photo[data-background-image]")
    photo = photo_el.get("data-background-image") if photo_el else None
    if photo:
        photo = photo.split("?")[0]

    return {
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre,
        "type_bien": type_bien,
        "ville_card": ville,
        "pieces": pieces,
        "surface": surface,
        "prix": prix,
        "photo": photo,
    }


async def _build_bien(
    client: httpx.AsyncClient, card: dict, cibles: list[str]
) -> dict | None:
    """Récupère le CP autoritaire + enrichit via la page détail, post-filtre dept."""
    try:
        r = await client.get(card["url"])
    except Exception:
        return None
    if r.status_code != 200:
        return None
    body = r.text

    cp = ""
    m_cp = re.search(r'itemprop="postalCode">\s*(\d{5})', body)
    if m_cp:
        cp = m_cp.group(1)

    # Post-filtre département STRICT (0 fuite hors-zone).
    if not cp or cp[:2] not in cibles:
        return None
    dept = cp[:2]

    locality = ""
    m_loc = re.search(r'itemprop="addressLocality">\s*([^<]+?)\s*<', body)
    if m_loc:
        locality = html.unescape(m_loc.group(1)).strip()

    realty = _parse_realty_json(body)

    # Localité propre : addressLocality > town (realty) > ville carte
    ville = locality or realty.get("town") or card.get("ville_card") or ""
    ville = re.sub(r"\s*/.*$", "", ville).strip()  # "ST MALO / ST MALO /" → "ST MALO"

    surface = _to_float(realty.get("surface")) or card.get("surface")
    surface_terrain = _to_float(realty.get("surface_land"))
    pieces = _to_int(realty.get("number_room")) or card.get("pieces")
    chambres = _to_int(realty.get("number_bedroom"))
    prix = _to_float(realty.get("price")) or card.get("prix")
    reference = (realty.get("reference") or "").strip() or card["id_annonce"]
    description = (realty.get("description") or "").strip()
    dpe = (realty.get("energy_consumption") or "").strip() or None
    if dpe and not re.fullmatch(r"[A-G]", dpe):
        dpe = None

    photos = _detail_photos(body) or ([card["photo"]] if card.get("photo") else [])

    titre = card.get("titre") or f"{card['type_bien'].title()} {ville}".strip()

    return {
        "source": "guenno",
        "url": card["url"],
        "id_annonce": str(reference),
        "titre": titre[:150],
        "type_bien": card["type_bien"],
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": dpe,
        "agence": "Guenno Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_realty_json(body: str) -> dict:
    """Extrait le bloc `var realty = {...};` (JSON immo-facile) par équilibrage
    des accolades (le JSON contient des chaînes avec accolades/`;`). {} si échec."""
    # `var realty = ` (et non `var realtys = ` qui le précède)
    m = re.search(r"var\s+realty\s*=\s*\{", body)
    if not m:
        return {}
    start = m.end() - 1  # position du '{' ouvrant
    depth = 0
    in_str = False
    esc = False
    end = None
    for i in range(start, len(body)):
        ch = body[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return {}
    try:
        data = json.loads(body[start:end])
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _detail_photos(body: str) -> list[str]:
    urls = re.findall(
        r"https://media\.immo-facile\.com/office/guenno/catalog/images/[^\s\"'?]+a\.jpg",
        body,
    )
    out: list[str] = []
    for u in urls:
        if u not in out:
            out.append(u)
    return out


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(str(v).replace(",", ".").replace(" ", ""))
        return f if f > 0 else None
    except (ValueError, TypeError):
        return None


def _to_int(v) -> int | None:
    f = _to_float(v)
    return int(f) if f is not None else None


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
    print(f"\nTotal Guenno: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']} — DPE {b['dpe']}"
        )

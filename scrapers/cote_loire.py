"""scrapers/cote_loire.py — Côté Loire Immobilier (agence Loches, 37)

Méthode : scrape_simple (httpx) — SSR HTML (plateforme LBI / staticlbi.com)
URL pattern : /vente/indre-et-loire/{page}   (~10 cartes/page, ~31 biens)
  ⚠️ Le segment "indre-et-loire" NE filtre PAS côté serveur : l'agence est
     mono-département (Indre-et-Loire, 37, autour de Loches) ; toutes les
     annonces sont en 37. On scrape donc la liste, puis POST-FILTRE strict sur
     code_postal[:2] ∈ départements cibles → 0 fuite (37 est ciblé).

Cartes (liste) : .card_bien
  - URL   : a[href]  → /vente/{id-ville}/{type}/{id-slug}
  - Loc   : .card_bien__localisation  →  "Ville (CODEPOSTAL)"
  - Prix  : .card_bien__prix          →  "312 000 €"
  - Titre : h2 / .card_bien__titre

Détail (enrichissement) : JSON-LD schema.org/Product
  - name  : "Maison 7pièce(s) 6chambre(s) 222 m² Le Grand-Pressigny (37350)"
  - image : liste d'URL CDN //cote-loire.staticlbi.com/...
  → pièces / chambres / surface extraits du name ; description depuis la page.

Type de bien : segment d'URL (/maison/, /propriete/, /maison-de-village/...).
  On garde maisons / propriétés / longères / fermes / manoirs / châteaux.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import html
import json
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.cote-loire.com"
LISTING_PATH = "/vente/indre-et-loire/{page}"
MAX_PAGES = 8
PHOTOS_PER_BIEN = 10
CONCURRENCY = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|fermette|longere|longère|manoir|"
    r"chateau|château|moulin|demeure|domaine|gite|gîte|maison-de-village",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|immeuble|local|commerce|garage|parking|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        cards = await _collect_cards(client)
        print(f"[CoteLoire] {len(cards)} cartes collectées")

        # Pré-filtre : dept (post-filtre strict) + type via slug
        retained: list[dict] = []
        for c in cards:
            cp = c["code_postal"]
            if not cp or cp[:2] not in departements:
                continue
            if _EXCLUDE_TYPE.search(c["type_seg"]) and not _KEEP_TYPE.search(
                c["type_seg"]
            ):
                continue
            if not _KEEP_TYPE.search(c["type_seg"]):
                continue
            retained.append(c)

        print(f"[CoteLoire] {len(retained)} cartes en zone (type OK)")

        sem = asyncio.Semaphore(CONCURRENCY)

        async def _enrich(card: dict) -> dict | None:
            async with sem:
                try:
                    bien = await _build_bien(client, card)
                except Exception as e:
                    print(f"[CoteLoire] Erreur fiche {card['url']}: {e}")
                    return None
                await asyncio.sleep(0.4)
                return bien

        biens = await asyncio.gather(*[_enrich(c) for c in retained])

    for bien in biens:
        if not bien:
            continue
        # Re-vérification dept STRICT (sécurité)
        cp = bien.get("code_postal") or ""
        if not cp or cp[:2] not in departements:
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

    print(f"[CoteLoire] {len(results)} biens retenus (zone + bornes)")
    return results


# ── Collecte des cartes ───────────────────────────────────────────────────────

async def _collect_cards(client: httpx.AsyncClient) -> list[dict]:
    cards: list[dict] = []
    seen: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        url = BASE_URL + LISTING_PATH.format(page=page)
        r = await client.get(url)
        if r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        page_cards = soup.select(".card_bien")
        new = 0
        for c in page_cards:
            loc_el = c.select_one(".card_bien__localisation")
            if not loc_el:
                continue
            a = c.find("a", href=True)
            href = a["href"] if a else ""
            if not href:
                continue
            curl = href if href.startswith("http") else BASE_URL + href
            if curl in seen:
                continue
            seen.add(curl)

            ville, cp = _parse_loc(loc_el.get_text(" ", strip=True))
            prix_el = c.select_one(".card_bien__prix")
            prix = _parse_price(prix_el.get_text(" ", strip=True)) if prix_el else None
            titre_el = c.select_one("h2") or c.select_one(".card_bien__titre")
            titre = titre_el.get_text(" ", strip=True) if titre_el else ""

            # Type depuis le segment d'URL : /vente/{id-ville}/{type}/{id-slug}
            parts = [p for p in href.split("/") if p]
            type_seg = parts[2] if len(parts) > 2 else ""
            type_seg = re.sub(r"^\d+-", "", type_seg)

            cards.append(
                {
                    "url": curl,
                    "ville": ville,
                    "code_postal": cp,
                    "prix": prix,
                    "titre": titre,
                    "type_seg": type_seg,
                }
            )
            new += 1
        if new == 0:
            break
        await asyncio.sleep(0.5)
    return cards


# ── Enrichissement détail ─────────────────────────────────────────────────────

async def _build_bien(client: httpx.AsyncClient, card: dict) -> dict | None:
    r = await client.get(card["url"])
    if r.status_code != 200:
        return None
    t = r.text
    soup = BeautifulSoup(t, "html.parser")

    product = _extract_product(t)
    name = ""
    photos: list[str] = []
    if product:
        name = html.unescape((product.get("name") or "").strip())
        imgs = product.get("image")
        if isinstance(imgs, list):
            photos = [_abs_img(u) for u in imgs if isinstance(u, str)]
        elif isinstance(imgs, str):
            photos = [_abs_img(imgs)]
    photos = list(dict.fromkeys(photos))[:PHOTOS_PER_BIEN]

    # pièces / chambres / surface depuis le name JSON-LD ("7pièce(s) 6chambre(s) 222 m²")
    pieces = _first_int(r"(\d+)\s*pi[eè]ce", name)
    chambres = _first_int(r"(\d+)\s*chambre", name)
    surface = _first_float(r"(\d[\d\s\xa0]{1,4})\s*m²", name)

    if surface is None:
        surface = _first_float(r"(\d[\d\s\xa0]{1,4})\s*m²", t)
    if pieces is None:
        pieces = _first_int(r"(\d+)\s*pi[eè]ces?", t)
    if chambres is None:
        chambres = _first_int(r"(\d+)\s*chambres?", t)

    # Description
    description = ""
    desc_el = (
        soup.select_one(".bien__description")
        or soup.select_one("[itemprop='description']")
        or soup.select_one(".description")
    )
    if desc_el:
        description = desc_el.get_text(" ", strip=True)
    if not description:
        og = soup.find("meta", attrs={"property": "og:description"})
        if og:
            description = og.get("content", "")
    description = html.unescape(description)

    surface_terrain = _parse_terrain(description) or _parse_terrain(name)

    if not photos:
        for img in soup.select("img[data-src], img[src]"):
            src = img.get("data-src") or img.get("src") or ""
            if "staticlbi" in src or "/biens/" in src:
                photos.append(_abs_img(src))
        photos = list(dict.fromkeys(photos))[:PHOTOS_PER_BIEN]

    type_bien = card["type_seg"].replace("-", " ").strip() or "maison"
    titre = name or card["titre"] or f"{type_bien.title()} {card['ville']}"
    id_annonce = card["url"].rstrip("/").rsplit("/", 1)[-1]
    cp = card["code_postal"]

    return {
        "source": "cote_loire",
        "url": card["url"],
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": cp[:2] if cp else None,
        "ville": card["ville"][:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": card["prix"],
        "photos": photos,
        "dpe": None,
        "agence": "Côté Loire Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_product(html_text: str) -> dict | None:
    for m in re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html_text, re.S
    ):
        try:
            data = json.loads(m.strip())
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if isinstance(it, dict) and it.get("@type") == "Product":
                return it
    return None


def _abs_img(u: str) -> str:
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return BASE_URL + u
    return u


def _parse_loc(text: str) -> tuple[str, str]:
    cp = ""
    m = re.search(r"\((\d{5})\)", text)
    if m:
        cp = m.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    if v is not None and v < 1000:
        return None
    return v


def _first_int(pattern: str, text: str) -> int | None:
    if not text:
        return None
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _first_float(pattern: str, text: str) -> float | None:
    if not text:
        return None
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
        return f if 8 <= f <= 5000 else None
    except ValueError:
        return None


def _parse_terrain(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*hectare", text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(",", ".")) * 10000
        except ValueError:
            pass
    m = re.search(
        r"terrain[^0-9]{0,25}(\d[\d\s\xa0]{2,6})\s*m²", text, re.IGNORECASE
    )
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            return float(val)
        except ValueError:
            pass
    return None


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
    print(f"\nTotal Côté Loire: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

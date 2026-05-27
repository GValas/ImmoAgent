"""
scrapers/laresidence.py — La Résidence Immobilier (réseau agences indépendantes)
Méthode : httpx pur — SSR HTML
URL : /achat/maison/{slug}/  →  paginé avec ?page=N
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re
import json
import httpx
from bs4 import BeautifulSoup

BASE = "https://www.laresidence.fr"

DEPT_SLUGS = {
    "72": "sarthe-72",
    "28": "eure-et-loir-28",
    "45": "loiret-45",
    "89": "yonne-89",
    "49": "maine-et-loire-49",
    "37": "indre-et-loire-37",
    "36": "indre-36",
    "18": "cher-18",
    "58": "nievre-58",
    "41": "loir-et-cher-41",
    "53": "mayenne-53",
    "44": "loire-atlantique-44",
    "85": "vendee-85",
    "35": "ille-et-vilaine-35",
    "61": "orne-61",
}

# URL patterns à essayer dans l'ordre
_URL_PATTERNS = [
    "/achat/maison/{slug}/",
    "/annonces/achat/maison/{slug}/",
    "/vente/maison/{slug}/",
    "/annonces/vente/maison/{slug}/",
    "/immobilier/achat/maison/{slug}/",
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

MAX_PAGES = 8


def _re_float(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text.replace("\xa0", " "))
    if not m:
        return None
    try:
        return float(m.group(1).replace(" ", "").replace(",", "."))
    except Exception:
        return None


def _re_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _re_str(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).upper() if m else None


def _parse_jsonld(html: str) -> list[dict]:
    for raw in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
        try:
            data = json.loads(raw)
            if data.get("@type") in ("CollectionPage", "ItemList"):
                items = data.get("mainEntity", {}).get("itemListElement", [])
                if items:
                    return items
        except Exception:
            continue
    return []


def _from_jsonld(item: dict, dept: str) -> dict | None:
    inner = item.get("item", item)
    url = inner.get("url", "")
    if not url:
        return None
    url = url if url.startswith("http") else BASE + url

    prix = None
    try:
        prix = float((inner.get("offers") or {}).get("price") or 0) or None
    except Exception:
        pass

    addr = inner.get("address", {})
    ville = addr.get("addressLocality", "")
    cp = addr.get("postalCode", "")
    if cp and not cp.startswith(dept):
        return None

    desc = inner.get("description", "") or ""
    titre = (inner.get("name", "") or "")[:150]
    images = inner.get("image", [])
    if isinstance(images, str):
        images = [images]
    photos = [i for i in images if isinstance(i, str) and i.startswith("http")][:8]

    surf_m = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", desc)
    surface = float(surf_m.group(1).replace(",", ".")) if surf_m else None

    id_m = re.search(r"/(\d{4,})", url)
    ad_id = id_m.group(1) if id_m else url.split("/")[-1] or url[-12:]

    return {
        "source": "laresidence",
        "url": url,
        "id_annonce": str(ad_id),
        "titre": titre or f"Maison — {ville}",
        "type_bien": "maison",
        "description": desc[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "La Résidence",
    }


def _parse_page(html: str, dept: str) -> list[dict]:
    # 1. JSON-LD
    json_items = _parse_jsonld(html)
    if json_items:
        return [b for item in json_items for b in [_from_jsonld(item, dept)] if b]

    # 2. HTML cards
    soup = BeautifulSoup(html, "html.parser")
    cards = (
        soup.select("div[class*='property-card']")
        or soup.select("article[class*='property']")
        or soup.select("div[class*='listing']")
        or soup.select("div[class*='annonce']")
        or soup.select("li[class*='bien']")
        or soup.select("article")
    )
    if len(cards) > 80:
        cards = [c for c in cards if "€" in c.get_text()]

    seen: set[str] = set()
    results = []
    for card in cards:
        try:
            b = _parse_card(card, dept)
            if b and b["url"] not in seen:
                seen.add(b["url"])
                results.append(b)
        except Exception:
            continue
    return results


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a[href]")
    if not link:
        return None
    href = link.get("href", "")
    if not href or href == "#":
        return None
    url = href if href.startswith("http") else BASE + href

    id_m = re.search(r"/(\d{4,})", href)
    ad_id = id_m.group(1) if id_m else href[-12:]

    text = card.get_text(" ", strip=True).replace("\xa0", " ")
    prix = _re_float(r"([\d][\d\s]*\d)\s*€", text)
    if not prix or prix < 10_000:
        return None

    surface = _re_float(r"(\d+(?:[.,]\d+)?)\s*m²", text)
    terrain_m = re.search(r"[Tt]errain\s+([\d\s]+)\s*m²", text)
    terrain = float(terrain_m.group(1).replace(" ", "")) if terrain_m else None
    pieces = _re_int(r"(\d+)\s*pièces?", text)
    chambres = _re_int(r"(\d+)\s*ch(?:ambres?)?", text)
    dpe = _re_str(r"\bDPE\s*:?\s*([A-G])\b", text)

    city_m = re.search(r"([A-ZÀ-Ÿa-zà-ÿ][^(]{2,30})\s*\((\d{5})\)", text)
    ville = city_m.group(1).strip() if city_m else ""
    cp = city_m.group(2) if city_m else ""
    if cp and not cp.startswith(dept):
        return None

    title_el = card.select_one("h2, h3, h4, [class*='title'], [class*='name']")
    titre = (title_el.get_text(strip=True) if title_el else f"Maison {ville}")[:150]

    photos = []
    for img in card.select("img"):
        for attr in ("src", "data-src", "data-lazy-src"):
            src = img.get(attr, "")
            if src and src.startswith("http"):
                photos.append(src)
                break
    photos = list(dict.fromkeys(photos))[:8]

    return {
        "source": "laresidence",
        "url": url,
        "id_annonce": str(ad_id),
        "titre": titre,
        "type_bien": "maison",
        "description": text[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "La Résidence",
    }


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max    = criteres.get("prix_max", 600_000)
    prix_min    = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 80)

    biens: list[dict] = []

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=20) as client:
        for dept in departements:
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                continue

            # Détection URL valide
            working_pattern: str | None = None
            for pattern in _URL_PATTERNS:
                url = BASE + pattern.format(slug=slug, dept=dept)
                try:
                    r = await client.get(url)
                    if r.status_code == 200:
                        parsed = _parse_page(r.text, dept)
                        if parsed:
                            working_pattern = pattern
                            for b in parsed:
                                biens.append(b)
                            print(f"[LaResidence] dept={dept} URL={url} → {len(parsed)} annonces")
                            break
                except Exception:
                    continue

            if not working_pattern:
                print(f"[LaResidence] dept={dept} — aucune URL valide")
                continue

            # Pages suivantes
            seen_ids = {b["id_annonce"] for b in biens}
            for page_num in range(2, MAX_PAGES + 1):
                base_url = (BASE + working_pattern.format(slug=slug, dept=dept)).rstrip("/")
                url = f"{base_url}?page={page_num}"
                try:
                    r = await client.get(url)
                    if r.status_code != 200:
                        break
                except Exception as e:
                    print(f"[LaResidence] ERR page {page_num}: {e}")
                    break

                parsed = _parse_page(r.text, dept)
                if not parsed:
                    break
                added = 0
                for b in parsed:
                    if b["id_annonce"] not in seen_ids:
                        seen_ids.add(b["id_annonce"])
                        biens.append(b)
                        added += 1
                print(f"[LaResidence] dept={dept} page={page_num} → {added} nouveaux")
                if added == 0:
                    break
                await asyncio.sleep(0.4)

    # Filtre final
    filtered = [
        b for b in biens
        if (not prix_max or not b.get("prix") or b["prix"] <= prix_max)
        and (not prix_min or not b.get("prix") or b["prix"] >= prix_min)
        and (not surface_min or not b.get("surface") or b["surface"] >= surface_min)
    ]
    print(f"[LaResidence] total: {len(filtered)} biens")
    return filtered


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria
    criteres = load_criteria()

    async def _test():
        result = await search({
            "departements": [72, 53, 28],
            "prix_max": criteres.prix_max,
            "prix_min": criteres.prix_min,
            "surface_min": criteres.surface_min,
        })
        print(f"\nTotal: {len(result)} annonces")
        for b in result[:5]:
            print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

    asyncio.run(_test())

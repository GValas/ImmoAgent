"""
scrapers/nexity.py — Nexity (réseau national, ancien + neuf)
Méthode : httpx + BeautifulSoup — SSR
URL : https://www.nexity.fr/annonces/achat/maison/{dept-slug}
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE = "https://www.nexity.fr"

_DEPT_SLUGS = {
    "72": "sarthe-72",    "28": "eure-et-loir-28", "45": "loiret-45",
    "89": "yonne-89",     "49": "maine-et-loire-49", "37": "indre-et-loire-37",
    "36": "indre-36",     "18": "cher-18",           "58": "nievre-58",
    "41": "loir-et-cher-41", "53": "mayenne-53",
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

MAX_PAGES = 4


def _re_float(pat, text):
    m = re.search(pat, text.replace("\xa0", " ").replace(" ", ""))
    try:
        return float(m.group(1).replace(",", ".")) if m else None
    except Exception:
        return None


def _parse_cards(html: str, dept: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = (
        soup.select("article.property-card")
        or soup.select("div[class*='PropertyCard']")
        or soup.select("div[class*='property-card']")
        or soup.select("div[class*='listing-card']")
        or soup.select("li[class*='property']")
        or [a.parent for a in soup.select("a[href*='/annonces/']") if a.parent]
    )

    results = []
    seen: set[str] = set()

    for card in cards:
        try:
            link = card.select_one("a[href]") if card.name != "a" else card
            if not link:
                continue
            href = link.get("href", "")
            if not href or href in ("#", "/"):
                continue
            url = href if href.startswith("http") else BASE + href
            if url in seen:
                continue

            text = card.get_text(" ", strip=True).replace("\xa0", " ")
            prix = _re_float(r"([\d]+[\d\s]*\d)\s*€", text)
            if not prix or prix < 10_000:
                continue

            surface = _re_float(r"(\d+(?:[.,]\d+)?)\s*m²", text)
            id_m = re.search(r"/(\d{4,})", href)
            ad_id = id_m.group(1) if id_m else href.rstrip("/").split("/")[-1]

            city_m = re.search(r"([A-ZÀ-Ÿa-zà-ÿ][^(]{2,30})\s*\((\d{5})\)", text)
            ville = city_m.group(1).strip()[:80] if city_m else ""
            cp    = city_m.group(2) if city_m else ""
            dept_f = cp[:2] if cp else dept

            titre_el = card.select_one("h2, h3, h4, [class*='title'], [class*='titre']")
            titre = (titre_el.get_text(strip=True) if titre_el else f"Maison {ville}")[:150]

            photos = []
            for img in card.select("img"):
                for attr in ("src", "data-src", "data-lazy"):
                    src = img.get(attr, "")
                    if src and src.startswith("http") and any(e in src.lower() for e in [".jpg", ".jpeg", ".webp", ".png"]):
                        photos.append(src)
                        break
            photos = list(dict.fromkeys(photos))[:8]

            pieces = None
            m = re.search(r"(\d+)\s*pièces?", text, re.IGNORECASE)
            if m: pieces = int(m.group(1))
            chambres = None
            m = re.search(r"(\d+)\s*ch(?:ambres?)?", text, re.IGNORECASE)
            if m: chambres = int(m.group(1))
            dpe = None
            m = re.search(r"\bDPE\s*:?\s*([A-G])\b", text, re.IGNORECASE)
            if m: dpe = m.group(1).upper()

            seen.add(url)
            results.append({
                "source": "nexity",
                "url": url,
                "id_annonce": str(ad_id),
                "titre": titre,
                "type_bien": "maison",
                "description": text[:1200],
                "departement": dept_f,
                "ville": ville,
                "code_postal": cp,
                "surface": surface,
                "surface_terrain": None,
                "pieces": pieces,
                "chambres": chambres,
                "prix": prix,
                "photos": photos,
                "dpe": dpe,
                "agence": "Nexity",
                "has_pool": bool(re.search(r"\bpiscine\b", text, re.IGNORECASE)),
            })
        except Exception:
            continue

    return results


async def _scrape_dept(client: httpx.AsyncClient, dept: str,
                       prix_min: float, prix_max: float, surface_min: float) -> list[dict]:
    slug = _DEPT_SLUGS.get(dept, f"departement-{dept}")
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        urls = [
            f"{BASE}/annonces/achat/maison/{slug}/",
            f"{BASE}/annonces/achat-maison-{slug}/",
            f"{BASE}/annonces/?type=maison&dept={dept}&p={page}",
        ] if page == 1 else [
            f"{BASE}/annonces/achat/maison/{slug}/?page={page}",
        ]

        found = False
        for url in urls:
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    continue
                cards = _parse_cards(r.text, dept)
                if not cards:
                    continue

                added = 0
                for b in cards:
                    if b["id_annonce"] in seen_ids:
                        continue
                    if prix_max and b.get("prix") and b["prix"] > prix_max:
                        continue
                    if prix_min and b.get("prix") and b["prix"] < prix_min:
                        continue
                    if surface_min and b.get("surface") and b["surface"] < surface_min:
                        continue
                    seen_ids.add(b["id_annonce"])
                    biens.append(b)
                    added += 1

                print(f"[Nexity] dept={dept} page={page} → {added} biens")
                found = True
                if added == 0:
                    return biens
                break
            except Exception as e:
                print(f"[Nexity] ERR dept={dept}: {e}")

        if not found:
            break

    return biens


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max    = criteres.get("prix_max", 600_000)
    prix_min    = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 80)

    results: list[dict] = []
    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=30) as client:
        tasks = [_scrape_dept(client, d, prix_min, prix_max, surface_min) for d in departements]
        for biens in await asyncio.gather(*tasks):
            results.extend(biens)

    print(f"[Nexity] total: {len(results)} biens")
    return results


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    result = asyncio.run(search({"departements": [72, 37], "prix_max": 550_000, "prix_min": 330_000, "surface_min": 150}))
    print(f"\nTotal: {len(result)} annonces")
    for b in result[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m²")

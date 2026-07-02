"""
scrapers/superimmo.py — Superimmo (agrégateur d'annonces d'agences)
Méthode : httpx + BeautifulSoup — SSR (réécrit 2026-07-02)
Le WAF renvoie 503 « Prouvez que vous êtes un humain » aux UA desktop mais laisse
passer un UA iPhone Safari. Filtres prix/surface acceptés en query params sur les
pages SSR : /achat/maison/{region}/{dept}?price_min=&price_max=&area_min=&sort=created_at
Pagination : /achat/maison/{region}/{dept}/p/{n}?…  (~15 cartes filtrées/page)
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import random
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import keep_bien, parse_price_digits, parse_str_upper

BASE = "https://www.superimmo.com"

# Le site range les départements sous leur région
_DEPT_PATHS = {
    "72": "pays-de-la-loire/sarthe",
    "53": "pays-de-la-loire/mayenne",
    "49": "pays-de-la-loire/maine-et-loire",
    "28": "centre-val-de-loire/eure-et-loir",
    "45": "centre-val-de-loire/loiret",
    "37": "centre-val-de-loire/indre-et-loire",
    "36": "centre-val-de-loire/indre",
    "18": "centre-val-de-loire/cher",
    "41": "centre-val-de-loire/loir-et-cher",
    "89": "bourgogne-franche-comte/yonne",
    "58": "bourgogne-franche-comte/nievre",
}

# UA mobile : le challenge anti-bot ne vise que les UA desktop
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                   "AppleWebKit/605.1.15 (Version/17.5 Mobile/15E148 Safari/604.1"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

MAX_PAGES = 6


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a[href^='/annonces/']")
    if not link:
        return None
    href = link.get("href", "")
    url = BASE + href
    ad_id = card.get("data-public-id") or href.rstrip("/").split("-")[-1]

    prix_el = card.select_one("b.prix")
    prix = parse_price_digits(prix_el.get_text() if prix_el else "")
    if not prix or prix < 10_000:
        return None

    titre_el = card.select_one("b.titre")
    titre = titre_el.get_text(" ", strip=True) if titre_el else "Maison"
    surface = None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", titre)
    if m:
        surface = float(m.group(1).replace(",", "."))
    pieces = None
    m = re.search(r"(\d+)\s*pièces?", titre)
    if m:
        pieces = int(m.group(1))
    chambres = None
    m = re.search(r"(\d+)\s*chambres?", titre)
    if m:
        chambres = int(m.group(1))
    terrain = None
    m = re.search(r"Ter\.\s*([\d\s \xa0]+)\s*m²", titre)
    if m:
        terrain = parse_price_digits(m.group(1))

    # CP/ville : depuis le slug (…-{ville}-{cp}-{id}) — fiable, présent partout
    cp, ville = "", ""
    m = re.search(r"-([a-z0-9-]+?)-(\d{5})-[a-z0-9]+$", href)
    if m:
        cp = m.group(2)
        ville = m.group(1).replace("-", " ").title()
    text = card.get_text(" ", strip=True)
    m = re.search(r"([A-ZÀ-Ÿ][a-zà-ÿA-ZÀ-Ÿ'’ -]{1,40}?)\s*\((\d{5})\)", text)
    if m:  # version accentuée si affichée dans la carte
        ville, cp = m.group(1).strip(), m.group(2)
    if not cp:
        return None

    dpe = None
    dpe_img = card.select_one(".dpe img[src*='dpe-short-arrow-']")
    if dpe_img:
        dpe = parse_str_upper(r"dpe-short-arrow-([a-g])-", dpe_img.get("src", ""))

    photos = []
    img = card.select_one(".slide img[src^='http']")
    if img:
        photos.append(img["src"])
    photos += [d["data-wide-photo-url"] for d in card.select("[data-wide-photo-url]")][:9]

    agence_el = card.select_one(".agency-name span")
    desc_el = card.select_one(".description, p.hidden-xs")
    return {
        "source": "superimmo",
        "url": url,
        "id_annonce": str(ad_id),
        "titre": f"{titre} {ville}"[:150],
        "type_bien": "maison",
        "description": (desc_el.get_text(" ", strip=True) if desc_el else text)[:1200],
        "departement": cp[:2],
        "ville": ville,
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": list(dict.fromkeys(photos))[:10],
        "dpe": dpe,
        "agence": agence_el.get_text(strip=True) if agence_el else "",
    }


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    qs = "sort=created_at"
    if prix_min:
        qs += f"&price_min={int(prix_min)}"
    if prix_max:
        qs += f"&price_max={int(prix_max)}"
    if surface_min:
        qs += f"&area_min={int(surface_min)}"

    results: list[dict] = []
    seen_ids: set = set()
    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=25) as client:
        for dept in departements:
            path = _DEPT_PATHS.get(dept)
            if not path:
                continue
            n_dept = 0
            for page in range(1, MAX_PAGES + 1):
                suffix = f"/p/{page}" if page > 1 else ""
                url = f"{BASE}/achat/maison/{path}{suffix}?{qs}"
                r = None
                for attempt in range(3):          # backoff long sur 429 (throttle serré)
                    try:
                        r = await client.get(url)
                    except Exception as e:
                        print(f"[Superimmo] Erreur dept {dept}: {e}")
                        break
                    if r.status_code != 429:
                        break
                    await asyncio.sleep(15 * (attempt + 1) * random.uniform(0.9, 1.3))
                if r is None:
                    break
                if r.status_code == 503:
                    print(f"[Superimmo] 503 anti-bot dept {dept} — abandon")
                    return results
                if r.status_code != 200:
                    print(f"[Superimmo] HTTP {r.status_code} dept {dept} p{page}")
                    break
                cards = BeautifulSoup(r.text, "html.parser").select("article.appart_view")
                added = 0
                for card in cards:
                    try:
                        b = _parse_card(card, dept)
                    except Exception:
                        continue
                    if b and keep_bien(b, dept, seen_ids, prix_max=prix_max,
                                       prix_min=prix_min, surface_min=surface_min):
                        results.append(b)
                        added += 1
                n_dept += added
                if not cards or added == 0:
                    break
                await asyncio.sleep(random.uniform(2.5, 5.0))
            print(f"[Superimmo] Dept {dept}: {n_dept} annonces")
            await asyncio.sleep(random.uniform(2.5, 5.0))

    print(f"[Superimmo] total: {len(results)} biens")
    return results


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Superimmo")

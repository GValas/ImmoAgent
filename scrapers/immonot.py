"""scrapers/immonot.py — Immonot (notaires immobilier)

Regional SSR pages. Price encoded in CDN image filenames and also
in <strong> inside .i-selection-card-price. Each region page shows
~20 listings filterable by dept code.

URL: /immobilier-notaire-{region}.html?departement={code}
"""

import asyncio
import re

import httpx

BASE = "https://www.immonot.com"

# target depts → (region slug, dept name suffix in URL slugs)
_DEPT_REGION = {
    "44": ("pays-de-la-loire", "loire-atlantique"),
    "49": ("pays-de-la-loire", "maine-et-loire"),
    "53": ("pays-de-la-loire", "mayenne"),
    "72": ("pays-de-la-loire", "sarthe"),
    "85": ("pays-de-la-loire", "vendee"),
    "18": ("centre", "cher"),
    "28": ("centre", "eure-et-loir"),
    "36": ("centre", "indre"),
    "37": ("centre", "indre-et-loire"),
    "41": ("centre", "loir-et-cher"),
    "45": ("centre", "loiret"),
    "58": ("bourgogne", "nievre"),
    "89": ("bourgogne", "yonne"),
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,*/*",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# CDN price extraction: city-postcode-dept-PRICE-euros-...jpg
_CDN_PRICE_RE = re.compile(
    r'-(\d{4,9})-euros-', re.IGNORECASE
)
_CDN_POSTCODE_RE = re.compile(r'-(\d{5})-')
_CDN_DEPT_RE = re.compile(r'-(\d{5})-[a-z]')

# HTML price: 659 925&nbsp;€ or 96 000 €
_HTML_PRICE_RE = re.compile(r'([\d][\d\s\xa0 ]{2,}[\d])\s*(?:&nbsp;)?€')


def _parse_cards(html: str, dept_code: str) -> list[dict]:
    """Extract listing cards from a region page, filtered to dept_code."""
    dept_info = _DEPT_REGION.get(dept_code)
    dept_slug_suffix = dept_info[1] if dept_info else ""

    results = []
    # Each card: <a href="..." class="i-selection-card ..." data-src="...">...</a>
    for m in re.finditer(
        r'<a\s[^>]*class="[^"]*i-selection-card[^"]*"[^>]*>(.*?)</a>',
        html,
        re.DOTALL,
    ):
        block = m.group(0)
        href_m = re.search(r'href="(/annonce-immobiliere/[^"]+)"', block)
        if not href_m:
            continue
        href = href_m.group(1)

        # Filter by dept: either postcode in data-src or dept name in href slug
        data_src_m = re.search(r'data-src="([^"]+)"', block)
        data_src = data_src_m.group(1) if data_src_m else ""

        in_dept = False
        postcode = None
        if data_src and "cdn-immonot" in data_src:
            cp_m = _CDN_POSTCODE_RE.search(data_src)
            if cp_m:
                postcode = cp_m.group(1)
                if postcode.startswith(dept_code):
                    in_dept = True
        if not in_dept and dept_slug_suffix and f"-{dept_slug_suffix}" in href:
            in_dept = True

        if not in_dept:
            continue

        # Skip rentals and terrains
        if "location-" in href or "terrain" in href:
            continue

        # Price: try CDN filename first, then HTML
        prix = None
        if data_src:
            price_m = _CDN_PRICE_RE.search(data_src)
            if price_m:
                prix = int(price_m.group(1))
        if not prix:
            strong_m = re.search(
                r'i-selection-card-price[^>]*>.*?<strong>(.*?)</strong>',
                block,
                re.DOTALL,
            )
            if strong_m:
                raw = re.sub(r'<[^>]+>', '', strong_m.group(1))
                price_m2 = _HTML_PRICE_RE.search(raw)
                if price_m2:
                    prix = int(re.sub(r'\D', '', price_m2.group(1)))

        if not prix or prix < 5000:
            continue

        # City from .i-selection-card-city
        city_m = re.search(r'i-selection-card-city[^>]*>([^<]+)<', block)
        ville_raw = city_m.group(1).strip() if city_m else ""
        # Remove " (XX)" dept suffix
        ville = re.sub(r'\s*\(\d+\)\s*$', '', ville_raw)

        # Photo URL
        if data_src:
            photo_url = (
                "https:" + data_src if data_src.startswith("//") else data_src
            )
        else:
            photo_url = None

        # Code postal from CDN or postcode variable
        if not postcode:
            if data_src:
                cp_m2 = _CDN_POSTCODE_RE.search(data_src)
                postcode = cp_m2.group(1) if cp_m2 else None

        results.append(
            {
                "titre": f"Maison à vendre — {ville}",
                "prix": prix,
                "surface": None,
                "pieces": None,
                "ville": ville,
                "code_postal": postcode or "",
                "departement": dept_code,
                "latitude": None,
                "longitude": None,
                "url": BASE + href,
                "photo_url": photo_url,
                "source": "immonot",
                "date_ajout": "",
            }
        )
    return results


async def search(criteres: dict) -> list[dict]:
    departements = [str(d) for d in criteres.get("departements", [])]
    biens: list[dict] = []

    # Group depts by region to avoid duplicate requests
    region_depts: dict[str, list[str]] = {}
    for dept in departements:
        info = _DEPT_REGION.get(dept)
        if not info:
            print(f"[Immonot] no region mapping for dept {dept}, skipping")
            continue
        region = info[0]
        region_depts.setdefault(region, []).append(dept)

    async with httpx.AsyncClient(
        headers=_HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for region, depts in region_depts.items():
            for dept in depts:
                url = f"{BASE}/immobilier-notaire-{region}.html?departement={dept}"
                try:
                    r = await client.get(url)
                except Exception as e:
                    print(f"[Immonot] ERR {url}: {e}")
                    continue
                cards = _parse_cards(r.text, dept)
                biens.extend(cards)
                print(f"[Immonot] dept={dept} region={region} → {len(cards)} biens")
                await asyncio.sleep(0.5)

    print(f"[Immonot] total: {len(biens)} biens")
    return biens


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    async def _test():
        results = await search({"departements": [72, 28, 53, 37]})
        for b in results[:8]:
            print(f"  {b['ville']} ({b['code_postal']}) — {b['prix']}€ — {b['url']}")

    asyncio.run(_test())

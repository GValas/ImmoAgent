"""scrapers/rightmove_overseas.py — Rightmove (section Overseas, biens en France)

Méthode : scrape_simple (httpx) — SSR via __NEXT_DATA__ (JSON Next.js inline)
URL : /overseas-property-for-sale/find.html?locationIdentifier=WORLD_REGION^{id}&index={N}
       → un identifiant WORLD_REGION DÉDIÉ par département (filtre serveur fiable,
         vérifié 0 fuite sur les 11 depts cibles). Pagination par `index` (25/page).
Données : props.pageProps.searchResults.properties[] (id, displayAddress avec CP,
          price.amount EUR + displayPrices €, propertySubType, bedrooms, location
          lat/lng EXACTES — utiles pour la géoloc du pipeline).
Particularités :
  - Agrège des centaines d'agences anglophones vendant en France.
  - Le prix EUR est fourni nativement (price.currencyCode=EUR ou displayPrices €).
  - La surface (displaySize) est souvent absente → None (ré-extraite ailleurs depuis
    le texte si besoin). Post-filtre dept STRICT sur le CP quand il est présent ;
    sinon on garde (WORLD_REGION garantit déjà le département).

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import json
import re

from scrapers._base import get_with_retry, make_client

BASE = "https://www.rightmove.co.uk/overseas-property-for-sale/find.html"
MAX_PAGES = 8          # 25/page → jusqu'à 200 biens/dept
PAGE_SIZE = 25

# département cible → identifiant WORLD_REGION Rightmove (vérifié 0 fuite)
WORLD_REGION: dict[str, str] = {
    "72": "160245", "53": "160058", "45": "156387", "89": "163081",
    "36": "156469", "18": "156249", "58": "163312", "28": "156317",
    "49": "160326", "37": "156601", "41": "156535",
}

_NEXT_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_KEEP_TYPE = re.compile(
    r"house|maison|propert|villa|farm|barn|manor|chateau|château|cottage|"
    r"longere|longère|mill|estate|country", re.IGNORECASE)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)
    results: list[dict] = []

    async with make_client(headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"),
        "Accept-Language": "en-GB,en;q=0.9",
    }) as client:
        for dept in departements:
            wid = WORLD_REGION.get(dept)
            if not wid:
                continue
            seen: set[str] = set()
            count = 0
            for page in range(MAX_PAGES):
                index = page * PAGE_SIZE
                url = (f"{BASE}?locationIdentifier=WORLD_REGION%5E{wid}"
                       f"&index={index}")
                r = await get_with_retry(client, url)
                if r is None or r.status_code != 200:
                    break
                props = _extract_props(r.text)
                if not props:
                    break
                new = 0
                for p in props:
                    bien = _parse_prop(p, dept)
                    if not bien or bien["id_annonce"] in seen:
                        continue
                    # post-filtre dept STRICT si CP présent
                    cp = bien.get("code_postal") or ""
                    if cp and cp[:2] != dept:
                        continue
                    seen.add(bien["id_annonce"])
                    pr = bien.get("prix") or 0
                    s = bien.get("surface") or 0
                    if prix_max and pr and pr > prix_max:
                        continue
                    if prix_min and pr and pr < prix_min:
                        continue
                    if surface_min and s and s < surface_min:
                        continue
                    results.append(bien)
                    new += 1
                count += new
                if len(props) < PAGE_SIZE:
                    break
                await asyncio.sleep(0.5)
            print(f"[Rightmove] Dept {dept}: {count} annonces")
            await asyncio.sleep(0.6)

    return results


def _extract_props(html: str) -> list[dict]:
    m = _NEXT_RE.search(html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
        return data["props"]["pageProps"]["searchResults"]["properties"]
    except Exception:
        return []


def _parse_prop(p: dict, dept: str) -> dict | None:
    pid = p.get("id")
    if pid is None:
        return None
    rel = p.get("propertyUrl") or f"/properties/{pid}"
    url = "https://www.rightmove.co.uk" + rel.split("?")[0].split("#")[0]

    subtype = p.get("propertySubType") or ""
    if subtype and not _KEEP_TYPE.search(subtype):
        # exclut Apartment/Land/Plot/Commercial
        if re.search(r"apartment|flat|land|plot|commercial|garage", subtype, re.I):
            return None

    addr = p.get("displayAddress") or ""
    cpm = re.search(r"\b(\d{5})\b", addr)
    code_postal = cpm.group(1) if cpm else ""
    ville = addr.split(",")[0].strip()

    price = p.get("price") or {}
    prix = None
    if price.get("currencyCode") == "EUR" and price.get("amount"):
        prix = float(price["amount"])
    else:
        for dp in price.get("displayPrices", []):
            mm = re.search(r"€\s*([\d,]+)", dp.get("displayPrice", ""))
            if mm:
                prix = float(mm.group(1).replace(",", ""))
                break

    photos = []
    for img in (p.get("images") or [])[:10]:
        u = img.get("srcUrl") or img.get("url")
        if u:
            photos.append(u)

    return {
        "source": "rightmove_overseas",
        "url": url,
        "id_annonce": str(pid),
        "titre": (subtype + " " + addr).strip()[:150],
        "type_bien": subtype.lower() or "maison",
        "description": (p.get("summary") or "")[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": None,
        "surface_terrain": None,
        "pieces": p.get("bedrooms"),
        "chambres": p.get("bedrooms"),
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": ((p.get("customer") or {}).get("branchDisplayName") or "Rightmove"),
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Rightmove")

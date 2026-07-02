"""
scrapers/stephaneplaza.py — Stéphane Plaza Immobilier (réseau national)
Méthode : scrape_js (Playwright) + interception du JSON interne.

Réécrit le 2026-07-02 : le réseau a désormais un vrai portail national sur
stephaneplazaimmobilier.com (l'ancien stephaneplaza.com est mort). Les pages
/acheter/departement/{slug}_{code}/maison/ (404 si aucune agence dans le dept)
chargent les annonces via GET /search-goods (JSON riche : prix, codePostal,
surface, surface-land, room/bedroom, consoEner=DPE, lat/lon). Cet endpoint est
protégé par Cloudflare sous httpx (403 même avec cookies+XSRF) mais passe sous
navigateur → on navigue en Playwright et on intercepte la réponse JSON.
Détail : /agences/{url_agency}/acheter/bien/{slug-du-nom}_{id}
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re
import unicodedata

from playwright.async_api import async_playwright

from scrapers._base import DEFAULT_DEPT_SLUGS, keep_bien, parse_price_digits

BASE_URL = "https://www.stephaneplazaimmobilier.com"

MAX_PAGES = 5          # par département (30 annonces/page)
PAGE_WAIT_S = 9        # attente du XHR search-goods après domcontentloaded


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = int(criteres.get("prix_max") or 0)
    prix_min = int(criteres.get("prix_min") or 0)
    surface_min = int(criteres.get("surface_min") or 0)

    results: list[dict] = []
    seen_ids: set = set()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            locale="fr-FR",
        )
        for dept in departements:
            slug = DEFAULT_DEPT_SLUGS.get(dept)
            if not slug:
                continue
            try:
                kept = 0
                for bien in await _scrape_dept(context, slug, dept):
                    if keep_bien(bien, dept, seen_ids,
                                 prix_max=prix_max, prix_min=prix_min,
                                 surface_min=surface_min):
                        results.append(bien)
                        kept += 1
                print(f"[StéphanePlaza] Dept {dept}: {kept} annonces")
            except Exception as e:
                print(f"[StéphanePlaza] Erreur dept {dept}: {e}")
        await browser.close()

    return results


async def _scrape_dept(context, slug: str, dept: str) -> list[dict]:
    """Navigue les pages /acheter/departement/{slug}_{dept}/maison/?page=N et
    intercepte le JSON /search-goods (la page 404 n'émet pas ce XHR → skip)."""
    biens: list[dict] = []
    for page_num in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/acheter/departement/{slug}_{dept}/maison/"
        if page_num > 1:
            url += f"?page={page_num}"

        payloads: list[dict] = []

        async def on_response(resp, _sink=payloads):
            if "search-goods" in resp.url and resp.status == 200:
                try:
                    _sink.append(await resp.json())
                except Exception:
                    pass

        page = await context.new_page()
        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
            for _ in range(PAGE_WAIT_S * 2):
                if payloads:
                    break
                await asyncio.sleep(0.5)
            await asyncio.sleep(1)   # laisse finir le json() en cours
        finally:
            try:
                await page.close()
            except Exception:
                pass

        if not payloads:
            break                    # 404 (pas d'agence dans le dept) ou XHR absent

        data = payloads[0]
        for rec in data.get("results") or []:
            bien = _parse_record(rec, dept)
            if bien:
                biens.append(bien)

        last_page = int((data.get("pagination") or {}).get("last_page") or 1)
        if page_num >= last_page:
            break
    return biens


def _slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "bien"


def _num_from(text) -> float | None:
    m = re.search(r"([\d\s]+(?:[.,]\d+)?)", str(text or "").replace("\xa0", " "))
    if not m:
        return None
    try:
        return float(m.group(1).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _parse_record(rec: dict, dept: str) -> dict | None:
    props = rec.get("properties") or {}
    prix = parse_price_digits(str(rec.get("price") or props.get("price") or ""))
    if not prix or prix < 10_000:
        return None

    cp = str(props.get("codePostal") or "")
    ville = str(props.get("city") or "").title()

    dpe = str(props.get("consoEner") or "").upper()
    if dpe not in {"A", "B", "C", "D", "E", "F", "G"}:
        dpe = None

    ad_id = str(rec.get("id") or "")
    agency = str(rec.get("url_agency") or "").strip("/")
    url = (
        f"{BASE_URL}/agences/{agency}/acheter/bien/{_slugify(rec.get('name'))}_{ad_id}"
        if agency and ad_id else BASE_URL
    )

    photos = [u for u in (rec.get("thumbnails") or []) if isinstance(u, str)][:10]
    loc = rec.get("location") or {}

    bien = {
        "source": "stephaneplaza",
        "url": url,
        "id_annonce": ad_id or url,
        "titre": str(rec.get("name") or "Maison")[:150],
        "type_bien": "maison",
        "description": str(rec.get("description") or rec.get("short_description") or "")[:2000],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": _num_from(props.get("surface")),
        "surface_terrain": _num_from(props.get("surface-land")),
        "pieces": int(props["room"]) if props.get("room") else None,
        "chambres": int(props["bedroom"]) if props.get("bedroom") else None,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": f"Stéphane Plaza Immobilier {agency}".strip()[:100],
    }
    if loc.get("lat") and loc.get("lon"):
        bien["latitude"] = float(loc["lat"])
        bien["longitude"] = float(loc["lon"])
    return bien


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "StéphanePlaza")

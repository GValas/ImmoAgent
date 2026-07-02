"""
scrapers/leboncoin.py — LeBonCoin (ventes immobilières)
Méthode : api_inoff — POST https://api.leboncoin.fr/finder/search (JSON).

⚠️ Protection = DataDome. Situation revérifiée le 2026-07-02 :
  - le site web (www.leboncoin.fr/recherche) ET l'API sous en-têtes navigateur
    renvoient 403 + interstitiel captcha-delivery → voie web MORTE sans proxy ;
  - MAIS l'API finder/search répond 200 avec le fingerprint de l'app ANDROID
    (User-Agent "LBC;Android;…" + header api_key public de l'app). Filtres
    serveur (price/square/real_estate_type/department) et pagination par offset
    fonctionnent ; JSON riche (attributes: rooms, bedrooms, land_plot_surface,
    energy_rate…), zéro fuite de département constatée.
  - Prudence DataDome : throttle + budget de pages/département + ARRÊT du run
    entier au premier 403 (insister ferait flagger l'IP, cf. seloger.py).
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import random

import httpx

from scrapers._base import keep_bien

API_URL = "https://api.leboncoin.fr/finder/search"

# Fingerprint app Android (l'API key est celle, publique, embarquée dans l'app).
APP_HEADERS = {
    "User-Agent": "LBC;Android;13;Pixel 7;phone;3f6f2f4a5e1b2c3d;wifi;100.14.0;10014000;0",
    "Accept": "application/json",
    "Content-Type": "application/json; charset=UTF-8",
    "api_key": "ba0c2dad52b3ec",
}

PAGE_SIZE = 35            # taille de page de l'app
MAX_PAGES_PER_DEPT = 8    # budget requêtes/département (~280 annonces max)
THROTTLE_S = 2.5          # délai moyen entre requêtes (réputation IP DataDome)

DEPARTEMENTS_CIBLES = ["72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"]


class _DataDomeBlocked(Exception):
    """L'IP est flaggée par DataDome (403) — inutile de continuer ce run."""


def _payload(dept: str, prix_min: int, prix_max: int, surface_min: int, offset: int) -> dict:
    ranges: dict = {}
    if prix_min or prix_max:
        price: dict = {}
        if prix_min:
            price["min"] = prix_min
        if prix_max:
            price["max"] = prix_max
        ranges["price"] = price
    if surface_min:
        ranges["square"] = {"min": surface_min}
    return {
        "filters": {
            "category": {"id": "9"},                                  # Ventes immobilières
            "enums": {"ad_type": ["offer"], "real_estate_type": ["1"]},  # Maison
            "ranges": ranges,
            "location": {"locations": [
                {"locationType": "department", "department_id": dept.lstrip("0") or dept},
            ]},
        },
        "limit": PAGE_SIZE,
        "offset": offset,
        "sort_by": "time",
        "sort_order": "desc",
    }


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = int(criteres.get("prix_max") or 0)
    prix_min = int(criteres.get("prix_min") or 0)
    surface_min = int(criteres.get("surface_min") or 0)

    results: list[dict] = []
    seen_ids: set = set()
    async with httpx.AsyncClient(timeout=25, headers=APP_HEADERS, follow_redirects=True) as client:
        try:
            for dept in departements:
                kept = 0
                for bien in await _scrape_dept(client, dept, prix_min, prix_max, surface_min):
                    if keep_bien(bien, dept, seen_ids,
                                 prix_max=prix_max, prix_min=prix_min,
                                 surface_min=surface_min):
                        results.append(bien)
                        kept += 1
                print(f"[LeBonCoin] Dept {dept}: {kept} annonces")
        except _DataDomeBlocked:
            print(f"[LeBonCoin] DataDome a flaggé l'IP (403) — run interrompu "
                  f"({len(results)} annonces conservées avant blocage).")
    return results


async def _scrape_dept(
    client: httpx.AsyncClient, dept: str, prix_min: int, prix_max: int, surface_min: int
) -> list[dict]:
    out: list[dict] = []
    for page in range(MAX_PAGES_PER_DEPT):
        try:
            r = await client.post(
                API_URL, json=_payload(dept, prix_min, prix_max, surface_min, page * PAGE_SIZE)
            )
        except httpx.HTTPError:
            break
        if r.status_code == 403 or "captcha-delivery" in r.text[:500]:
            raise _DataDomeBlocked()
        if r.status_code != 200:
            break
        try:
            data = r.json()
        except ValueError:
            break
        ads = data.get("ads") or []
        for ad in ads:
            bien = _parse_ad(ad, dept)
            if bien:
                out.append(bien)
        if len(ads) < PAGE_SIZE:
            break
        await asyncio.sleep(THROTTLE_S * random.uniform(0.7, 1.4))
    return out


def _parse_ad(ad: dict, dept: str) -> dict | None:
    prix_list = ad.get("price") or []
    prix = float(prix_list[0]) if prix_list else None
    if not prix:
        return None

    attrs = {a.get("key"): a for a in ad.get("attributes", []) if a.get("key")}

    def attr(key: str) -> str:
        a = attrs.get(key)
        return str(a.get("value") or "") if a else ""

    def attr_num(key: str) -> float | None:
        v = attr(key).replace(",", ".")
        try:
            return float(v) if v else None
        except ValueError:
            return None

    loc = ad.get("location") or {}
    cp = str(loc.get("zipcode") or "")
    ville = str(loc.get("city") or "")

    surface = attr_num("square")
    terrain = attr_num("land_plot_surface")
    pieces = attr_num("rooms")
    chambres = attr_num("bedrooms")

    dpe = attr("energy_rate").upper()
    if dpe not in {"A", "B", "C", "D", "E", "F", "G"}:
        dpe = None

    images = ad.get("images") or {}
    photos = [u for u in (images.get("urls") or []) if isinstance(u, str)][:10]

    owner = ad.get("owner") or {}
    agence = str(owner.get("name") or "") if owner.get("type") == "pro" else ""

    ad_id = str(ad.get("list_id") or "")
    url = ad.get("url") or (f"https://www.leboncoin.fr/ad/ventes_immobilieres/{ad_id}" if ad_id else "")

    bien = {
        "source": "leboncoin",
        "url": url,
        "id_annonce": ad_id or url,
        "titre": str(ad.get("subject") or "Maison")[:150],
        "type_bien": "maison",
        "description": str(ad.get("body") or "")[:2000],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": terrain,
        "pieces": int(pieces) if pieces else None,
        "chambres": int(chambres) if chambres else None,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": agence[:100],
    }
    lat, lng = loc.get("lat"), loc.get("lng")
    if lat and lng:
        bien["latitude"] = float(lat)
        bien["longitude"] = float(lng)
    return bien


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "LeBonCoin")

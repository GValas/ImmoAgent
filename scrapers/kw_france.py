"""
scrapers/kw_france.py — Keller Williams France
Méthode : api_inoff (httpx) — le site a migré sur la plateforme Netty (netty.immo).

Réécrit le 2026-07-02 : plus de Playwright. Le front (React CSR) charge TOUT le
catalogue national via POST /webapi/getJson/Templates/ProductsList (pages de 500,
`searchCount` en tête) puis filtre côté client — on rejoue cet appel en httpx et
on filtre par préfixe de code postal en Python. Le `componentHash` exigé par
l'API est l'id du div[data-type="component"][data-author="Netty.fr"] présent
dans le HTML SSR de /vente/maison (extrait à chaque run — sans lui l'API répond
success:1 mais 0 propriété). Le champ `hash` = b64(type_offer:1)_b64(prod_type:house).
Détail : https://www.kwfrance.com/vente/{url.fr}
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import base64
import re

import httpx

from scrapers._base import keep_bien, make_client
from scrapers._base import parse_str_upper as _re_str

BASE = "https://www.kwfrance.com"
API_URL = f"{BASE}/webapi/getJson/Templates/ProductsList"
LIST_PAGE = f"{BASE}/vente/maison"

PAGE_SIZE = 500
MAX_PAGES = 8          # garde-fou (catalogue ~750 maisons en 2026-07)
THROTTLE_S = 2.0

API_HEADERS = {
    "Content-Type": "application/json",
    "Origin": BASE,
    "Referer": LIST_PAGE,
    "X-Requested-With": "XMLHttpRequest",
}


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode().rstrip("=")


def _payload(component_hash: str, offset: int) -> dict:
    return {
        "componentHash": component_hash,
        "offset": offset,
        "limit": PAGE_SIZE,
        "username": "company54466zys",
        "hash": f"{_b64('type_offer:1')}_{_b64('prod_type:house')}",
        "params": {"type_offer": "1", "prod_type": "house", "query": {}},
        "pageType": "ProductsList",
        "lang": "fr",
    }


async def _component_hashes(client: httpx.AsyncClient) -> list[str]:
    """Ids des composants Netty du HTML SSR (candidats componentHash)."""
    r = await client.get(LIST_PAGE)
    if r.status_code != 200:
        return []
    return re.findall(
        r'data-type="component"[^>]*id="([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"',
        r.text,
    )


async def _fetch_page(client: httpx.AsyncClient, component_hash: str, offset: int) -> dict | None:
    """Une page du catalogue ; retourne data ou None. Retry simple (504 vus)."""
    for attempt in range(2):
        try:
            r = await client.post(API_URL, json=_payload(component_hash, offset), headers=API_HEADERS)
        except httpx.HTTPError:
            r = None
        if r is not None and r.status_code == 200:
            try:
                j = r.json()
            except ValueError:
                return None
            if j.get("success"):
                return j.get("data") or {}
            return None
        if attempt == 0:
            await asyncio.sleep(4)
    return None


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = int(criteres.get("prix_max") or 0)
    prix_min = int(criteres.get("prix_min") or 0)
    surface_min = int(criteres.get("surface_min") or 0)

    results: list[dict] = []
    seen_ids: set = set()
    par_dept: dict[str, int] = {}

    async with make_client(timeout=90) as client:
        hashes = await _component_hashes(client)
        if not hashes:
            print("[KWFrance] componentHash introuvable dans le HTML SSR — abandon")
            return []

        # Trouve le bon composant (celui qui renvoie des propriétés).
        component_hash, data = None, None
        for h in hashes:
            d = await _fetch_page(client, h, 0)
            if d and d.get("frontProperties"):
                component_hash, data = h, d
                break
            await asyncio.sleep(THROTTLE_S)
        if not data:
            print("[KWFrance] API ProductsList sans propriétés — structure changée ?")
            return []

        total = int(data.get("searchCount") or 0)
        offset = 0
        pages = 0
        while data and pages < MAX_PAGES:
            props = data.get("frontProperties") or {}
            for prod_ref, p in props.items():
                bien = _parse_prop(prod_ref, p)
                if not bien:
                    continue
                dept = bien["code_postal"][:2]
                if dept not in departements:
                    continue
                if keep_bien(bien, dept, seen_ids,
                             prix_max=prix_max, prix_min=prix_min,
                             surface_min=surface_min):
                    results.append(bien)
                    par_dept[dept] = par_dept.get(dept, 0) + 1
            pages += 1
            offset += PAGE_SIZE
            if offset >= total or not props:
                break
            await asyncio.sleep(THROTTLE_S)
            data = await _fetch_page(client, component_hash, offset)

    for dept in sorted(par_dept):
        print(f"[KWFrance] Dept {dept}: {par_dept[dept]} annonces")
    print(f"[KWFrance] total: {len(results)} biens")
    return results


def _strip_html(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html or "", flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]{2,}", " ", text.replace("&nbsp;", " ")).strip()


def _parse_prop(prod_ref: str, p: dict) -> dict | None:
    cp = str(p.get("cp") or "")
    if len(cp) != 5:
        return None
    prix = p.get("price2") or (p.get("formated") or {}).get("price", {}).get("amount")
    try:
        prix = float(prix)
    except (TypeError, ValueError):
        return None      # "Nous contacter" / prix masqué
    if not prix:
        return None

    slug = (p.get("url") or {}).get("fr") or ""
    url = f"{BASE}/vente/{slug}" if slug else LIST_PAGE

    titre = p.get("title_auto") or (p.get("title") or {}).get("fr") or f"Maison {p.get('city', '')}"
    description = _strip_html((p.get("details") or {}).get("fr") or "")[:2000]
    dpe = _re_str(r"\bDPE\s*:?\s*\(?([A-G])\)?\b", f"{titre} {description}")

    photos = [u for u in (p.get("photos") or []) if isinstance(u, str)][:10]

    def _num(key: str) -> float | None:
        v = p.get(key)
        try:
            return float(v) if v else None
        except (TypeError, ValueError):
            return None

    return {
        "source": "kw_france",
        "url": url,
        "id_annonce": str(prod_ref),
        "titre": str(titre)[:150],
        "type_bien": "maison",
        "description": description,
        "departement": cp[:2],
        "ville": str(p.get("city") or "")[:80],
        "code_postal": cp,
        "surface": _num("surface"),
        "surface_terrain": _num("land"),
        "pieces": int(p["rooms"]) if p.get("rooms") else None,
        "chambres": int(p["rooms2"]) if p.get("rooms2") else None,
        "prix": float(prix),
        "photos": photos,
        "dpe": dpe,
        "agence": "Keller Williams",
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "KWFrance")

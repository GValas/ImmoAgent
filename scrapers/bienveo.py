"""scrapers/bienveo.py — Bienveo (Union sociale pour l'habitat — ventes des bailleurs sociaux)

Méthode : scrape_simple (httpx) — Next.js, données SSR dans __NEXT_DATA__
URL pattern : /rechercher/vente-maison-{dept-slug}-{NN}?page=N
              → filtre type+département CÔTÉ SERVEUR (Elasticsearch embarqué dans
              props.pageProps.defaultSearchResponse), ~15 hits/page.
Chaque hit._source.data porte code_postal / ville / departement / surface_habitable /
nb_pieces_logement / nombre_de_chambres / dpe_etiquette_conso → post-filtre STRICT
code_postal[:2] via keep_bien. Page détail : /offre/{reference}.

Niche originale : ventes HLM (loi ELAN) des bailleurs sociaux — petits pavillons
40-150 k€, donc généralement 0 stock avec les bornes prod (prix_min 300 k€,
surface_min 150 m²) : scraper FONCTIONNEL 0 STOCK, utile si les critères changent.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import json
import re

from scrapers._base import DEFAULT_DEPT_SLUGS, get_with_retry, run_dept_api, standalone_main

BASE_URL = "https://www.bienveo.fr"
MAX_PAGES = 8

# Slug bienveo = « {nom-departement}-{NN} » (vente-maison-loiret-45)
DEPT_SLUGS = {dept: f"{slug}-{dept}" for dept, slug in DEFAULT_DEPT_SLUGS.items()}

_NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# Types de bien conservés (le slug maison filtre déjà côté serveur)
_KEEP_TYPE = re.compile(r"maison|pavillon|villa|ferme|longère|longere", re.IGNORECASE)


def _data_val(data: dict, code: str):
    """data.{code}.value du blob ES bienveo (None si absent)."""
    field = data.get(code) or {}
    return field.get("value")


def _to_float(v) -> float | None:
    try:
        f = float(str(v).replace(",", "."))
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _parse_hit(src: dict, dept: str) -> dict | None:
    data = src.get("data") or {}
    ptype = (src.get("productType") or {}).get("description") or ""
    if ptype and not _KEEP_TYPE.search(ptype):
        return None

    reference = src.get("reference") or ""
    if not reference:
        return None

    cp = str(_data_val(data, "code_postal") or "")
    ville = str(_data_val(data, "ville") or "")
    dpe = _data_val(data, "dpe_etiquette_conso")
    dpe = str(dpe).upper() if dpe and str(dpe).upper() in "ABCDEFG" else None

    photos = []
    for pic in ((src.get("mediaSupports") or {}).get("pictures") or []):
        u = pic.get("url") or pic.get("path") or ""
        if u:
            photos.append(u)

    prix = _to_float((src.get("transaction") or {}).get("price"))

    bien = {
        "source": "bienveo",
        "url": f"{BASE_URL}/offre/{reference}",
        "id_annonce": str(src.get("id") or reference),
        "titre": (src.get("title") or "")[:150],
        "type_bien": (ptype or "maison").lower(),
        "description": (src.get("description") or "")[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": _to_float(_data_val(data, "surface_habitable")),
        "surface_terrain": None,
        "pieces": int(_data_val(data, "nb_pieces_logement") or 0) or None,
        "chambres": int(_data_val(data, "nombre_de_chambres") or 0) or None,
        "prix": prix,
        "photos": photos[:10],
        "dpe": dpe,
        "agence": (src.get("advertiser") or {}).get("nom"),
    }
    loc = src.get("location") or {}
    if loc.get("lat") and loc.get("lon"):
        bien["latitude"] = float(loc["lat"])
        bien["longitude"] = float(loc["lon"])
    return bien


async def _fetch_dept(client, dept: str, slug: str) -> list[dict]:
    biens: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/rechercher/vente-maison-{slug}"
        if page > 1:
            url += f"?page={page}"
        r = await get_with_retry(client, url)
        if r is None or r.status_code != 200:
            break
        m = _NEXT_RE.search(r.text)
        if not m:
            break
        try:
            payload = json.loads(m.group(1))
            hits = (payload["props"]["pageProps"]["defaultSearchResponse"]
                    ["hits"]["hits"])
        except (KeyError, TypeError, ValueError):
            break
        if not hits:
            break
        for hit in hits:
            try:
                bien = _parse_hit(hit.get("_source") or {}, dept)
            except Exception:
                continue
            if bien:
                biens.append(bien)
        if len(hits) < 12:          # page incomplète = dernière (~15/page)
            break
        await asyncio.sleep(0.5)
    return biens


async def search(criteres: dict) -> list[dict]:
    return await run_dept_api(
        source="bienveo",
        label="Bienveo",
        fetch_dept=_fetch_dept,
        criteres=criteres,
        dept_slugs=DEPT_SLUGS,
    )


if __name__ == "__main__":
    standalone_main(search, "Bienveo")

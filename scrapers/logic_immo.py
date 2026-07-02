"""
scrapers/logic_immo.py — Logic-Immo (plateforme AVIV / SeLoger Group)
Méthode : httpx + API JSON interne (réécrit 2026-07-02)
  1. POST /serp-bff/search           → ids des annonces (filtres prix/surface serveur)
  2. GET  /classifiedList/{id,id,…}  → données complètes par lots de 30
Le site a migré sur la plateforme AVIV (mêmes MFEs qu'immowelt.de) : les anciennes
URLs /vente-immobilier-{slug} redirigent vers la homepage. DataDome protège le site
mais laisse passer httpx avec un UA desktop réaliste (Playwright headless est bloqué).
Les départements utilisent des place IDs AVIV internes (AD06FRxx ≠ numéro dept).
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import random

from scrapers._base import HEADERS, run_dept_api, standalone_main

BASE = "https://www.logic-immo.com"
SEARCH_URL = f"{BASE}/serp-bff/search"
CARDS_URL = f"{BASE}/classifiedList/"

# Place IDs AVIV niveau département (AD06FR<idx> — index interne, PAS le n° dept ;
# mapping relevé via GET /serp-bff/places?placesIds[]=… le 2026-07-02)
DEPT_PLACE_IDS = {
    "72": "AD06FR73",   # Sarthe
    "28": "AD06FR27",   # Eure-et-Loir
    "45": "AD06FR46",   # Loiret
    "89": "AD06FR90",   # Yonne
    "49": "AD06FR50",   # Maine-et-Loire
    "37": "AD06FR38",   # Indre-et-Loire
    "36": "AD06FR37",   # Indre
    "18": "AD06FR18",   # Cher
    "58": "AD06FR59",   # Nièvre
    "41": "AD06FR42",   # Loir-et-Cher
    "53": "AD06FR54",   # Mayenne
}

PAGE_SIZE = 100      # taille max constatée OK côté serp-bff
MAX_PAGES = 3        # ≤300 annonces/dept (tri DateDesc → les plus récentes d'abord)
CARDS_BATCH = 30     # taille de lot du frontend officiel

_blocked = False     # coupe-circuit DataDome (403) — stoppe tous les depts suivants


def _parse_card(d: dict, dept: str) -> dict | None:
    if not isinstance(d, dict) or d.get("status") not in (None, "Published"):
        return None
    raw = d.get("rawData") or {}
    addr = (d.get("location") or {}).get("address") or {}
    cp = str(addr.get("zipCode") or "")
    if not cp:                       # pas de CP → garde-fou département impossible
        return None
    surface = (raw.get("surface") or {}).get("main")
    terrain = (raw.get("surface") or {}).get("plot")
    desc = d.get("mainDescription") or {}
    description = " — ".join(x for x in (desc.get("headline"), desc.get("description")) if x)
    photos = [img.get("url") for img in (d.get("gallery") or {}).get("images") or []
              if isinstance(img, dict) and img.get("url")][:10]
    provider = d.get("provider") or {}
    agence = ((provider.get("intermediaryCard") or {}).get("title")
              or (provider.get("contactCard") or {}).get("title") or "")
    legacy_id = (d.get("metadata") or {}).get("legacyId")
    url = d.get("url") or (f"{BASE}/detail-vente-{legacy_id}.htm" if legacy_id else "")
    titre = (d.get("hardFacts") or {}).get("title") or "Maison"
    ville = addr.get("city") or ""
    if ville and ville not in titre:
        titre = f"{titre} {ville}"
    return {
        "source": "logic_immo",
        "url": url,
        "id_annonce": str(d.get("id") or legacy_id or ""),
        "titre": titre[:150],
        "type_bien": str(raw.get("propertyTypeLabel") or "maison").lower(),
        "description": description[:1200],
        "departement": cp[:2],
        "ville": ville,
        "code_postal": cp,
        "surface": float(surface) if surface else None,
        "surface_terrain": float(terrain) if terrain else None,
        "pieces": raw.get("nbroom"),
        "chambres": raw.get("nbbedroom"),
        "prix": float(raw.get("price")) if raw.get("price") else None,
        "photos": photos,
        "dpe": d.get("energyClass") or None,
        "agence": agence,
    }


async def _fetch_dept(client, dept: str, place_id: str | None) -> list[dict]:
    global _blocked
    if _blocked or not place_id:
        return []
    criteres = getattr(client, "_li_criteres", {})
    criteria: dict = {
        "distributionTypes": ["Buy"],
        "estateTypes": ["House"],
        "location": {"placeIds": [place_id]},
    }
    if criteres.get("prix_min"):
        criteria["priceMin"] = int(criteres["prix_min"])
    if criteres.get("prix_max"):
        criteria["priceMax"] = int(criteres["prix_max"])
    if criteres.get("surface_min"):
        criteria["spaceMin"] = int(criteres["surface_min"])

    # 1) ids paginés (tri DateDesc, filtres serveur)
    ids: list[str] = []
    for page in range(1, MAX_PAGES + 1):
        body = {"criteria": criteria,
                "paging": {"page": page, "size": PAGE_SIZE, "order": "DateDesc"}}
        r = await client.post(SEARCH_URL, json=body)
        if r.status_code == 403:
            _blocked = True
            print(f"[LogicImmo] 403 DataDome sur serp-bff/search (dept {dept}) — abandon")
            return []
        if r.status_code != 200:
            print(f"[LogicImmo] HTTP {r.status_code} search dept {dept} p{page}")
            break
        data = r.json()
        batch = [c.get("id") for c in data.get("classifieds") or [] if c.get("id")]
        ids.extend(batch)
        if len(batch) < PAGE_SIZE or len(ids) >= data.get("totalCount", 0):
            break
        await asyncio.sleep(random.uniform(0.8, 1.8))

    # 2) données complètes par lots
    biens: list[dict] = []
    for i in range(0, len(ids), CARDS_BATCH):
        chunk = ids[i:i + CARDS_BATCH]
        r = await client.get(CARDS_URL + ",".join(chunk))
        if r.status_code == 403:
            _blocked = True
            print(f"[LogicImmo] 403 DataDome sur classifiedList (dept {dept}) — abandon")
            break
        if r.status_code != 200:
            print(f"[LogicImmo] HTTP {r.status_code} classifiedList dept {dept}")
            continue
        for card in r.json():
            b = _parse_card(card, dept)
            if b:
                biens.append(b)
        await asyncio.sleep(random.uniform(0.8, 1.8))
    return biens


async def search(criteres: dict) -> list[dict]:
    global _blocked
    _blocked = False
    headers = {
        **HEADERS,
        "Accept": "application/json",
        "x-language": "fr",
        "Origin": BASE,
        "Referer": f"{BASE}/classified-search",
    }

    async def fetch_dept(client, dept, slug):
        client._li_criteres = criteres          # passe prix/surface au fetch
        return await _fetch_dept(client, dept, slug)

    return await run_dept_api(
        source="logic_immo",
        label="LogicImmo",
        fetch_dept=fetch_dept,
        criteres=criteres,
        dept_slugs=DEPT_PLACE_IDS,
        dept_sleep=1.2,
        client_kwargs={"headers": headers, "timeout": 30},
    )


if __name__ == "__main__":
    standalone_main(search, "LogicImmo")

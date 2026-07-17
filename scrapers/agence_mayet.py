"""scrapers/agence_mayet.py — Agence Mayet / Agence de la Vallée de la Creuse
(Argenton-sur-Creuse, 36 — agence familiale FNAIM depuis 1978)

Méthode : scrape_simple (httpx) — CMS **Netty** (React CSR) : l'état complet de
la page est embarqué en base64 dans un <script> inline (b64_to_utf8("…")) :
  - prodResults["search"] = réfs des biens de la page (ex "VM630")
  - prodId[ref]           = fiche produit COMPLÈTE (cp, city, pricePrimary,
                            surface, land, rooms, rooms2, details.fr, photos…)
Même patron que scrapers/immo_mais_pas_que.py.

URL pattern : /vente?page={N} (~6 biens/page, arrêt quand plus de réf nouvelle).
Stock mono-département Indre (36) → post-filtre STRICT code_postal[:2], 0 fuite.

Type : prod_type == "house" uniquement ; type_offer == 1 (vente).
Ne requête que si le 36 est demandé.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import base64
import json
import re

from scrapers._base import get_with_retry, keep_bien, make_client, standalone_main

BASE_URL = "https://www.mayet-immo.fr"
SOURCE = "agence_mayet"
LABEL = "AgenceMayet"
AGENCE = "Agence Mayet (Vallée de la Creuse)"
DEPTS_STOCK = {"36"}
MAX_PAGES = 25
PHOTOS_PER_BIEN = 10

_B64_RE = re.compile(r'b64_to_utf8\("([^"]+)"\)')


def _template_data(html: str) -> dict | None:
    """Extrait le blob _TEMPLATE_DATA (celui qui porte prodResults/prodId)."""
    for blob in _B64_RE.findall(html):
        try:
            data = json.loads(base64.b64decode(blob).decode("utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and "prodResults" in data and "prodId" in data:
            return data
    return None


def _parse_product(ref: str, p: dict) -> dict | None:
    if p.get("prod_type") != "house":        # appt / land / parking / pro…
        return None
    if p.get("type_offer") not in (1, None):  # 1 = Vente (2 loc, 3 viager)
        return None

    cp = str(p.get("cp") or "")
    formated = p.get("formated") or {}
    prix = p.get("pricePrimary") or (formated.get("price") or {}).get("amount")

    titre = p.get("title") or {}
    titre = titre.get("fr") if isinstance(titre, dict) else str(titre or "")
    details = p.get("details") or {}
    description = details.get("fr") if isinstance(details, dict) else str(details or "")

    slug = p.get("url") or {}
    slug = slug.get("fr") if isinstance(slug, dict) else str(slug or "")
    url = f"{BASE_URL}/vente/{slug},{ref}" if slug else f"{BASE_URL}/vente"

    photos = [u for u in (p.get("photos") or []) if isinstance(u, str)]

    type_house = str(p.get("type_house") or "")
    type_bien = {"01": "maison individuelle", "15": "maison ancienne"}.get(
        type_house, "maison"
    )

    return {
        "source": SOURCE,
        "url": url,
        "id_annonce": ref,
        "titre": (titre or f"Maison {p.get('city') or ''}").strip()[:150],
        "type_bien": type_bien,
        "description": (description or "")[:1200],
        "departement": cp[:2],
        "ville": str(p.get("city") or "")[:80],
        "code_postal": cp,
        "surface": p.get("surface") or None,
        "surface_terrain": p.get("land") or None,
        "pieces": p.get("rooms") or None,
        "chambres": p.get("rooms2") or None,
        "prix": float(prix) if prix else None,
        "photos": photos[:PHOTOS_PER_BIEN],
        "dpe": None,
        "agence": AGENCE,
    }


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    if not DEPTS_STOCK.intersection(departements):
        return []  # agence locale : rien à chercher hors de ses départements

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()
    seen_refs: set[str] = set()

    async with make_client() as client:
        for page in range(1, MAX_PAGES + 1):
            r = await get_with_retry(client, f"{BASE_URL}/vente?page={page}")
            if r is None or r.status_code != 200:
                break
            data = _template_data(r.text)
            if not data:
                break
            refs = data.get("prodResults", {}).get("search") or []
            new_refs = [x for x in refs if x not in seen_refs]
            if not new_refs:            # le listing boucle après la dernière page
                break
            seen_refs.update(new_refs)

            prod_by_ref = data.get("prodId") or {}
            for ref in new_refs:
                p = prod_by_ref.get(ref)
                if not isinstance(p, dict):
                    continue
                try:
                    bien = _parse_product(ref, p)
                except Exception:
                    continue
                if not bien:
                    continue
                cp = bien["code_postal"]
                # Post-filtre STRICT département → 0 fuite hors zone.
                if not cp or cp[:2] not in departements:
                    continue
                if not keep_bien(bien, cp[:2], seen_ids, prix_max=prix_max,
                                 prix_min=prix_min, surface_min=surface_min):
                    continue
                results.append(bien)

            await asyncio.sleep(0.5)

    print(f"[{LABEL}] {len(results)} annonces")
    return results


if __name__ == "__main__":
    standalone_main(search, AGENCE)

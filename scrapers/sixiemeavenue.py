"""scrapers/sixiemeavenue.py — Sixième Avenue (réseau M6 / plateforme SPF Tech)

STATUT : INACTIF (blacklist). Code conservé pour réactivation éventuelle via
Playwright/navigateur réel ou proxy résidentiel.

Architecture du site : SPA Vue.js (app.js ~3.8 Mo) sur backend Laravel mutualisé
avec Stéphane Plaza Immobilier (CSP/cookies *.spftech.net, *.spfwww.net,
*.stephaneplazaimmobilier.com). Le HTML de /acheter est une coquille CSR : 0 €,
0 carte exploitable. Toutes les annonces proviennent de deux endpoints XHR :

  GET /place-search?name={ville|cp|dept}&limit=50   → résout un lieu (id, polygone)
  GET /search-goods?{departement,city,page,perPage,propertyType,order,...}
                                                     → liste JSON des biens

Filtre département : l'app envoie `departement` (préfixe 2 chiffres du CP) au
endpoint /search-goods → filtre CÔTÉ SERVEUR théoriquement propre.

BLOCAGE (testé 2026-05-30) : les deux endpoints renvoient HTTP 403 corps vide
(server: cloudflare, cf-ray présent) même avec User-Agent Chrome, cookies de
session valides (sixieme_avenue_session + XSRF-TOKEN issus d'un GET /acheter
préalable), en-tête X-XSRF-TOKEN, X-Requested-With, Referer et en-têtes
sec-ch-ua/sec-fetch complets. Cloudflare Bot Management bloque au niveau edge
(empreinte TLS/JA3 non-navigateur) — httpx reproduit le 403 à l'identique.
Ce n'est PAS un 419 (CSRF Laravel) ni un challenge JS résoluble : corps de 0 octet.

→ Inscrit en blacklist (cf. sources.yaml). Réactivation possible uniquement avec
  Playwright (navigateur réel) ou proxy résidentiel + impersonation TLS
  (curl_cffi / tls-client). Le squelette ci-dessous est prêt à parser le JSON
  /search-goods le jour où l'accès est débloqué (mapping à valider sur réponse réelle).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx

BASE_URL = "https://www.sixiemeavenue.com"
SEARCH_GOODS = f"{BASE_URL}/search-goods"
PLACE_SEARCH = f"{BASE_URL}/place-search"
MAX_PAGES = 10
PER_PAGE = 24

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE_URL}/acheter",
}

# Types de bien à conserver (maisons / propriétés / manoirs / longères)
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|long[èe]re|manoir|ch[âa]teau|moulin|"
    r"demeure|domaine|mas|g[îi]te|corps[- ]de[- ]ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # Amorce des cookies de session (XSRF-TOKEN + sixieme_avenue_session)
        try:
            await client.get(f"{BASE_URL}/acheter")
        except Exception as e:
            print(f"[SixiemeAvenue] Amorce session échouée : {e}")
            return results

        for dept in departements:
            try:
                biens = await _scrape_dept(
                    client, dept, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[SixiemeAvenue] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[SixiemeAvenue] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        params = {
            "departement": dept,   # préfixe 2 chiffres du CP (filtre serveur)
            "page": page,
            "perPage": PER_PAGE,
            "order": "date-desc",
        }
        r = await client.get(SEARCH_GOODS, params=params)
        if r.status_code != 200:
            # 403 attendu tant que le blocage Cloudflare n'est pas contourné.
            print(
                f"[SixiemeAvenue] HTTP {r.status_code} sur /search-goods "
                f"(dept {dept}, page {page}) — accès bloqué (Cloudflare)."
            )
            break

        try:
            data = r.json()
        except Exception:
            break

        items = _extract_items(data)
        if not items:
            break

        new_on_page = 0
        for it in items:
            bien = _parse_item(it, dept)
            if not bien:
                continue
            if bien["code_postal"] and bien["code_postal"][:2] != dept:
                continue  # sécurité anti-fuite
            aid = bien["id_annonce"]
            if aid in seen:
                continue

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            seen.add(aid)
            biens.append(bien)
            new_on_page += 1

        if new_on_page == 0 or len(items) < PER_PAGE:
            break
        await asyncio.sleep(0.4)

    return biens


def _extract_items(data) -> list:
    """Localise la liste d'annonces dans la réponse JSON (structure à confirmer)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "goods", "results", "items", "biens", "hits"):
            v = data.get(key)
            if isinstance(v, list):
                return v
            if isinstance(v, dict) and isinstance(v.get("data"), list):
                return v["data"]
    return []


def _parse_item(it: dict, dept: str) -> dict | None:
    """Mapping JSON → dict Bien. À valider sur une réponse /search-goods réelle."""
    try:
        type_raw = str(
            it.get("type") or it.get("propertyType") or it.get("nature") or ""
        )
        if _EXCLUDE_TYPE.search(type_raw) and not _KEEP_TYPE.search(type_raw):
            return None
        if type_raw and not _KEEP_TYPE.search(type_raw):
            return None
        type_bien = type_raw or "maison"

        cp = str(
            it.get("postalCode") or it.get("zipcode") or it.get("code_postal") or ""
        )
        ville = it.get("city") or it.get("ville") or it.get("commune") or ""

        slug = it.get("slug") or it.get("id") or ""
        url = it.get("url") or (f"{BASE_URL}/acheter/bien/{slug}" if slug else BASE_URL)
        id_annonce = str(it.get("id") or it.get("reference") or slug or url)

        return {
            "source": "sixiemeavenue",
            "url": url,
            "id_annonce": id_annonce,
            "titre": str(it.get("title") or it.get("titre") or "")[:150],
            "type_bien": type_bien,
            "description": str(it.get("description") or "")[:1200],
            "departement": dept,
            "ville": str(ville)[:80],
            "code_postal": cp,
            "surface": _num(it.get("surface") or it.get("livingArea")),
            "surface_terrain": _num(it.get("landArea") or it.get("surfaceTerrain")),
            "pieces": _int(it.get("rooms") or it.get("pieces")),
            "chambres": _int(it.get("bedrooms") or it.get("chambres")),
            "prix": _num(it.get("price") or it.get("prix")),
            "dpe": it.get("dpe"),
            "photos": _photos(it),
            "agence": "Sixième Avenue",
        }
    except Exception:
        return None


def _photos(it: dict) -> list[str]:
    photos = it.get("photos") or it.get("images") or []
    out = []
    if isinstance(photos, list):
        for p in photos:
            if isinstance(p, str) and p.startswith("http"):
                out.append(p)
            elif isinstance(p, dict):
                u = p.get("url") or p.get("src") or ""
                if u.startswith("http"):
                    out.append(u)
    return out[:10]


def _num(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    cleaned = re.sub(r"[^\d.,]", "", str(v)).replace(",", ".")
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _int(v) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    biens = asyncio.run(
        search(
            {
                "departements": criteres.departements,
                "prix_max": criteres.prix_max,
                "prix_min": getattr(criteres, "prix_min", 0),
                "surface_min": criteres.surface_min,
            }
        )
    )
    print(f"\nTotal Sixième Avenue: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€ — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

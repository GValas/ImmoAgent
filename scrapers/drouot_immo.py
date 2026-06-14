"""scrapers/drouot_immo.py — Drouot.immo (ventes aux enchères immobilières interactives)

Plateforme d'enchères immobilières interactives lancée par la maison Drouot.
Couverture NATIONALE (biens vendus aux enchères, prix de départ).

Méthode : api_inoff (httpx pur) — Next.js : tout le catalogue est dans
le JSON `#__NEXT_DATA__` de la page /annonces (pas de Playwright nécessaire).
URL pattern : /annonces?page=N
  - `props.pageProps.adverts` : liste de 40 biens/page (champs propres :
    `id`, `zipCode`, `city`, `startingPrice`, `squareMeters`, `amountOfRooms`,
    `propertyType`, `labelFr`, `descriptionFr`).
  - `props.pageProps.total` : nombre total de biens → on pagine jusqu'à l'épuiser.

Filtre DÉPARTEMENT : aucun paramètre serveur ne filtre (testé : ?departement=NN /
  ?zipCode=NN laissent `total` inchangé). On récupère TOUT le catalogue national
  (≈ 2 pages) et on POST-FILTRE STRICT par `zipCode[:2]`. → 0 fuite garantie.

L'URL de détail (slug) n'est pas dans le JSON : on la reconstruit depuis les ancres
`<a href="/annonces/{slug}-{id}">` de la page (map id→href), sinon on retombe sur
l'URL de liste.

`prix` = `startingPrice` (prix de départ de l'enchère, honoraires inclus) pour rester
comparable aux autres sources. `descriptionFr` (complète) est conservée → utile au
filtre mots-clés et au match qualitatif.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://drouot.immo"
LISTING_PATH = "/annonces"
PER_PAGE = 40
MAX_PAGES = 12  # garde-fou (≈ 84 biens nationaux / 40 = 3 pages)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types à conserver (propertyType de l'API) : maison / villa / propriété…
_KEEP_TYPE = re.compile(
    r"maison|villa|propri[ée]t[ée]|ferme|long[èe]re|manoir|chateau|ch[âa]teau|"
    r"moulin|demeure|domaine|mas|corps[- ]de[- ]ferme|hotel particulier|h[ôo]tel",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|garage|parking|immeuble|local|commerce|bureau|"
    r"fonds|cave|box|studio|loft|parts",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}{LISTING_PATH}?page={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[DrouotImmo] ERR page {page}: {e}")
                break
            if r.status_code != 200:
                break

            soup = BeautifulSoup(r.text, "html.parser")
            nd = soup.select_one("#__NEXT_DATA__")
            if not nd:
                break
            try:
                data = json.loads(nd.string)
                pp = data["props"]["pageProps"]
            except (json.JSONDecodeError, KeyError):
                break

            adverts = pp.get("adverts") or []
            total = pp.get("total") or 0
            if not adverts:
                break

            # Map id → href de détail (slug) depuis les ancres de la page
            id2href = _build_href_map(soup)

            new_on_page = 0
            for adv in adverts:
                try:
                    bien = _parse_advert(adv, id2href, departements)
                except Exception:
                    continue
                if not bien:
                    continue
                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                new_on_page += 1

                # POST-FILTRE département STRICT
                cp = bien["code_postal"]
                if not cp or cp[:2] not in departements:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                results.append(bien)

            # Stop : plus rien de nouveau, ou on a couvert tout le catalogue
            if new_on_page == 0 or len(seen_ids) >= total:
                break
            await asyncio.sleep(0.4)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print(f"[DrouotImmo] total: {len(results)} biens (zone cible) — par dept: {by_dept}")
    return results


def _build_href_map(soup) -> dict[int, str]:
    out: dict[int, str] = {}
    for a in soup.select("a[href^='/annonces/']"):
        href = a.get("href", "")
        m = re.search(r"-(\d+)$", href)
        if m:
            out[int(m.group(1))] = href
    return out


def _parse_advert(adv: dict, id2href: dict, departements: set) -> dict | None:
    aid = adv.get("id")
    if aid is None:
        return None

    ptype = (adv.get("propertyType") or "").strip()
    if _EXCLUDE_TYPE.search(ptype) and not _KEEP_TYPE.search(ptype):
        return None
    if ptype and not _KEEP_TYPE.search(ptype):
        return None
    type_bien = ptype.replace("/", " ").strip() or "maison"

    cp = str(adv.get("zipCode") or "").strip()
    cp = re.sub(r"\D", "", cp).zfill(5)[:5] if cp else ""
    ville = (adv.get("city") or "").strip()
    dept = cp[:2] if cp else ""

    href = id2href.get(aid, "")
    url = (BASE_URL + href) if href else f"{BASE_URL}{LISTING_PATH}"

    prix = adv.get("startingPrice")
    try:
        prix = float(prix) if prix is not None else None
    except (TypeError, ValueError):
        prix = None

    surface = adv.get("squareMeters")
    try:
        surface = float(surface) if surface is not None else None
    except (TypeError, ValueError):
        surface = None

    pieces = adv.get("amountOfRooms")
    try:
        pieces = int(pieces) if pieces is not None else None
    except (TypeError, ValueError):
        pieces = None

    titre = (adv.get("labelFr") or "").strip() or f"{type_bien.title()} {ville}".strip()
    description = (adv.get("descriptionFr") or "").strip()

    return {
        "source": "drouot_immo",
        "url": url,
        "id_annonce": str(aid),
        "titre": titre[:150],
        "type_bien": type_bien[:60],
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": [],
        "dpe": None,
        "agence": "Drouot.immo (enchères)",
    }


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
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
    print(f"\nTotal Drouot.immo: {len(biens)} biens")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]} — {b['prix']}€"
            f" — {b.get('surface') or '?'}m² — {b.get('pieces') or '?'}p — {b['ville']}"
        )

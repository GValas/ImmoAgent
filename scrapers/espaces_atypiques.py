"""
scrapers/espaces_atypiques.py — Espaces Atypiques (propriétés de caractère)
Méthode : scrape_simple (httpx) — feed JSON statique (réactivé 2026-07-02).

L'ancienne piste « CSR React sans filtre dept » est contournée : la page /ventes/
charge en réalité un feed JSON STATIQUE de tout le stock national puis filtre côté
client (vu dans wp-content/themes/espaces_atypiques/js/liste-annonces.js) :
  /wp-content/plugins/tv2m-json-feed-annonces/annonces-vente-fr.json  (~4 Mo)
Chaque annonce expose cp/ville/prix/surface/chambres/lat/lng/url/img/ref/types_ids.
On télécharge le feed UNE fois et on post-filtre en Python : type 13 (= maison ;
12 = appartement, 2516 = terrain, 1527 = bateau), code_postal[:2] ∈ départements
cibles, bornes prix/surface via keep_bien. Prix parfois non numérique
(« Sous compromis ») → annonce ignorée. Coordonnées exactes fournies (lat/lng).
Interface : async def search(criteres: dict) -> list[dict]
"""
import html as htmllib

from scrapers._base import get_with_retry, keep_bien, make_client

FEED_URL = ("https://www.espaces-atypiques.com/wp-content/plugins/"
            "tv2m-json-feed-annonces/annonces-vente-fr.json")

TYPE_MAISON = 13


def _to_float(v) -> float | None:
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def _parse_annonce(a: dict) -> dict | None:
    if TYPE_MAISON not in (a.get("types_ids") or []):
        return None
    prix = _to_float(a.get("prix"))
    if not prix or prix < 10_000:        # « Sous compromis », vide…
        return None
    cp = str(a.get("cp") or "")
    if len(cp) != 5:
        return None

    titre = htmllib.unescape(a.get("nom") or "Maison atypique")
    ville = (a.get("ville") or "").title()
    bien = {
        "source": "espaces_atypiques",
        "url": a.get("url") or "",
        "id_annonce": str(a.get("ref") or a.get("url") or ""),
        "titre": titre[:150],
        "type_bien": "maison",
        "description": titre,
        "departement": cp[:2],
        "ville": ville[:80],
        "code_postal": cp,
        "surface": _to_float(a.get("surface")),
        "surface_terrain": None,
        "pieces": None,
        "chambres": int(a["chambres"]) if str(a.get("chambres") or "").isdigit() else None,
        "prix": prix,
        "photos": [a["img"]] if a.get("img") else [],
        "dpe": None,
        "agence": f"Espaces Atypiques {(a.get('agence') or '').title()}".strip(),
    }
    lat, lng = _to_float(a.get("lat")), _to_float(a.get("lng"))
    if lat and lng:
        bien["latitude"], bien["longitude"] = lat, lng
    return bien


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    async with make_client(timeout=60) as client:
        r = await get_with_retry(client, FEED_URL)
    if r is None or r.status_code != 200:
        print(f"[EspacesAtypiques] feed indisponible "
              f"(HTTP {r.status_code if r else 'ERR'})")
        return []
    try:
        annonces = r.json().get("annonces") or []
    except Exception as e:
        print(f"[EspacesAtypiques] feed illisible: {e}")
        return []

    results: list[dict] = []
    seen_ids: set = set()
    for a in annonces:
        try:
            bien = _parse_annonce(a)
        except Exception:
            continue
        if not bien or bien["departement"] not in departements:
            continue
        if keep_bien(bien, bien["departement"], seen_ids,
                     prix_max=prix_max, prix_min=prix_min, surface_min=surface_min):
            results.append(bien)

    for dept in sorted(departements):
        n = sum(1 for b in results if b["departement"] == dept)
        if n:
            print(f"[EspacesAtypiques] Dept {dept}: {n} annonces")
    return results


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "EspacesAtypiques")

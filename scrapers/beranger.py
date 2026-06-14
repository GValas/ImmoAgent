"""scrapers/beranger.py — Béranger Immobilier (agence indépendante à Tours, 37)

Méthode : scrape_simple (httpx) — SSR HTML (CMS maison « hosproduction »).
URL liste : /acheter/   (catalogue complet sur une page, ~36 biens, pas de
            pagination réelle : /acheter/page-N.html renvoie la même liste).
Cartes : div.col1
  - h3 > a.LinkIn[href*='/acheter/N-...']  → titre + URL détail
  - .reference          → "Référence : NNNN" (id_annonce)
  - .localisation       → libellé localité ("Tours Centre", "Fondettes", …)
  - .description        → accroche
  - .prixfai            → "2390000 € HAI"

PARTICULARITÉ : pas de code postal ni de surface en vue liste — seulement un
libellé de localité, souvent suffixé d'un quartier ("Tours Centre", "Tours
Halles"). Agence MONO-DÉPARTEMENT (Tours / Indre-et-Loire 37) → on normalise le
libellé en commune INSEE puis on résout commune → code_postal via l'API BAN
officielle (api-adresse.data.gouv.fr), avec POST-FILTRE STRICT code_postal[:2] ∈
departements → 0 fuite hors-zone. La surface est récupérée ensuite en page détail
par gallery.py.

Si aucun dept cible ∈ {37, 41} (limitrophes), on ne touche pas le site.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import make_client, parse_price

BASE_URL = "https://www.beranger-immobilier.com"
LIST_URL = f"{BASE_URL}/acheter/"
BAN_URL = "https://api-adresse.data.gouv.fr/search/"

_PERIMETRE = {"37", "41"}

_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|terrain|local|commerce|garage|parking|immeuble|"
    r"bureau|fonds|viager|duplex|loft|t[1-5]\b",
    re.IGNORECASE,
)
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|long[èe]re|manoir|ch[âa]teau|moulin|demeure|"
    r"domaine|h[ôo]tel particulier|gentilhommi|corps de ferme|ferme",
    re.IGNORECASE,
)

_CP_CACHE: dict[str, str | None] = {}


def _normalise_localite(loc: str) -> str:
    """'Tours Centre' / 'Tours Halles' → 'Tours' ; garde les communes composées."""
    loc = loc.strip()
    # Quartiers/suffixes connus à retirer après le nom de commune.
    loc = re.sub(
        r"\b(centre|hyper.?centre|halles|pr[ée]bendes|sainte.radegonde|nord|sud|"
        r"est|ouest|b[ée]ranger|gare|vieux.tours)\b.*$",
        "", loc, flags=re.IGNORECASE,
    ).strip()
    return loc or "Tours"


async def _ville_to_cp(client: httpx.AsyncClient, ville: str) -> str | None:
    key = ville.strip().lower()
    if not key:
        return None
    if key in _CP_CACHE:
        return _CP_CACHE[key]
    cp: str | None = None
    try:
        r = await client.get(
            BAN_URL, params={"q": ville, "type": "municipality", "limit": 1},
            timeout=15,
        )
        if r.status_code == 200:
            feats = r.json().get("features", [])
            if feats:
                props = feats[0].get("properties", {})
                cp = props.get("postcode") or None
                citycode = props.get("citycode") or ""
                if cp and citycode[:2] and citycode[:2] != cp[:2] and len(cp) == 5:
                    cp = citycode[:2] + cp[2:]
    except Exception:
        cp = None
    _CP_CACHE[key] = cp
    return cp


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)

    if departements and not (departements & _PERIMETRE):
        print("[Beranger] Aucun dept cible dans le périmètre 37/41 → skip")
        return []

    results: list[dict] = []
    seen: set[str] = set()

    async with make_client(timeout=25) as client:
        try:
            r = await client.get(LIST_URL)
        except Exception as e:
            print(f"[Beranger] Erreur accès liste : {e}")
            return []
        if r.status_code != 200:
            print(f"[Beranger] Liste status {r.status_code}")
            return []

        cards = BeautifulSoup(r.text, "html.parser").select("div.col1")
        raw: list[dict] = []
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if bien:
                raw.append(bien)

        for bien in raw:
            p = bien.get("prix") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue

            cp = await _ville_to_cp(client, bien["_localite_norm"])
            if not cp or cp[:2] not in departements:
                continue
            bien["code_postal"] = cp
            bien["departement"] = cp[:2]
            bien.pop("_localite_norm", None)

            aid = bien["id_annonce"]
            if aid in seen:
                continue
            seen.add(aid)
            results.append(bien)

    print(f"[Beranger] {len(results)} annonces dans la zone")
    return results


def _parse_card(card) -> dict | None:
    h3 = card.select_one("h3 a.LinkIn[href]") or card.select_one("a.LinkIn[href]")
    href = h3.get("href", "") if h3 else ""
    if not href or "/acheter/" not in href:
        return None
    if not re.search(r"/acheter/\d+", href):
        return None
    url = href if href.startswith("http") else BASE_URL + href

    titre = (h3.get("title") or h3.get_text(" ", strip=True)).strip()
    titre = re.sub(r"\s+", " ", titre)

    # exclusion par type (titre + slug d'URL)
    blob = titre + " " + href
    if _EXCLUDE_TYPE.search(blob) and not _KEEP_TYPE.search(blob):
        return None

    loc_el = card.select_one(".localisation")
    localite = loc_el.get_text(" ", strip=True) if loc_el else ""

    desc_el = card.select_one(".description")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    ref_el = card.select_one(".reference")
    ref = ""
    if ref_el:
        m = re.search(r"R[ée]f[ée]rence\s*:?\s*(\S+)", ref_el.get_text(" ", strip=True))
        ref = m.group(1) if m else ""
    if not ref:
        m = re.search(r"/acheter/(\d+)-", href)
        ref = m.group(1) if m else url
    id_annonce = ref

    price_el = card.select_one(".prixfai")
    prix = parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    photos = []
    img = card.select_one("img[src]")
    if img:
        src = img.get("src") or ""
        if src and not src.startswith("data:"):
            photos.append(src if src.startswith("http") else BASE_URL + src)

    return {
        "source": "beranger",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": description[:1200],
        "departement": None,
        "ville": _normalise_localite(localite)[:80] or localite[:80],
        "code_postal": "",
        "surface": None,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Béranger Immobilier",
        "_localite_norm": _normalise_localite(localite),
    }


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    biens = asyncio.run(search({
        "departements": criteres.departements,
        "prix_max": criteres.prix_max,
        "prix_min": getattr(criteres, "prix_min", 0),
        "surface_min": criteres.surface_min,
    }))
    print(f"\nTotal Béranger: {len(biens)} annonces")
    depts = sorted({str(b.get("code_postal") or "")[:2] for b in biens if b.get("code_postal")})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(f"  [{b.get('code_postal')}] {str(b.get('titre'))[:55]} — "
              f"{b.get('prix')}€ — {b.get('ville')}")

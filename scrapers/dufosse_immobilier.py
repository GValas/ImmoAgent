"""scrapers/dufosse_immobilier.py — Dufossé Immobilier (équestre / caractère / Lyon)

Site : https://www.dufosseimmobilier.com — agence Tassin-la-Demi-Lune (Rhône 69),
       spécialisée propriétés de caractère / villas / propriétés équestres. Le stock
       « équestre » est national ; le stock « maisons » est très majoritairement
       Rhône / Monts du Lyonnais (zone cible → 0 stock attendu).

Méthode : scrape_simple (httpx) — SSR HTML, CMS custom (templates/dufosse).

URL pattern (PAS de filtre département serveur) :
  - Listes par catégorie + pagination -wN :
      /maisons-w1 , /maisons-w2 ...            (12 cartes / page)
      /proprietes-equestres-w1 ...
  → on crawle toutes les pages et on POST-FILTRE par département.

Particularité ANTI-FUITE : le site n'expose AUCUN code postal au niveau du bien
(ni en liste, ni en page détail — seul le CP de l'agence, 69160, apparaît).
La localisation n'est donnée que sous forme de NOM de ville / département / région
dans le titre (« Ecully … », « Lot-et-Garonne : … », « … en Bourgogne du Sud »).
→ on RÉSOUT le département à partir de ces noms (référentiel DEPT_NOMS + alias
   régionaux) ; si le département reste indéterminé OU hors zone cible, le bien est
   EXCLU. Résultat : 0 fuite (un bien hors cible ou non localisable n'est jamais
   retenu).

Cartes (liste) : div.ann
  - URL    : h2.headline-ann a[href]  → details-{slug}-{id}
  - Titre  : h2.headline-ann a (porte la localisation textuelle)
  - Réf    : .numerodemandat
  - Prix   : .prix
  - Picts  : .pict.nombredepieces / .superficie (.font-weight-bold)
  - Photo  : .img--back img[data-src]

Enrichissement (page détail, sur les seuls biens en zone cible) :
  - Surface terrain (.pict.surfaceterrain), chambres (.pict.nombredechambres),
    DPE (« Note DPE X »), description (og:description), photos.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import sys
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.dept_data import DEPT_NOMS  # noqa: E402
from scrapers._base import (  # noqa: E402
    HEADERS,
    parse_int,
    parse_price,
    parse_surface,
    standalone_main,
)

BASE_URL = "https://www.dufosseimmobilier.com"
LIST_CATEGORIES = ["maisons", "proprietes-equestres"]
MAX_PAGES = 15
PHOTOS_PER_CARD = 10
DETAIL_CONCURRENCY = 4

# Types à conserver / exclure (sur titre + libellé catégorie)
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|long[èe]re|manoir|ch[âa]teau|moulin|"
    r"demeure|domaine|mas|corps de ferme|g[îi]te|haras|[ée]questre",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain\b|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|viager|loft|studio",
    re.IGNORECASE,
)

# ── Résolution département depuis un libellé textuel ────────────────────────────
# Nom de département (normalisé) → code, + quelques alias régionaux fréquents chez
# cette agence (les régions ne sont PAS des départements cibles → résolues vers ""
# implicitement : on ne mappe que ce qui tombe dans la zone cible ou est sans risque).


def _norm(s: str) -> str:
    s = (s or "").lower()
    for a, b in (("à", "a"), ("â", "a"), ("ä", "a"), ("é", "e"), ("è", "e"),
                 ("ê", "e"), ("ë", "e"), ("î", "i"), ("ï", "i"), ("ô", "o"),
                 ("ö", "o"), ("û", "u"), ("ü", "u"), ("ç", "c")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return f" {s.strip()} "


# Référentiel nom→code pour TOUS les départements (sert à détecter aussi les
# départements hors-zone, qu'on exclura ; un nom hors-zone évite un faux positif).
_NAME_TO_CODE: dict[str, str] = {}
for _code, _nom in DEPT_NOMS.items():
    _NAME_TO_CODE[_norm(_nom).strip()] = _code

# Quelques villes/secteurs récurrents de l'agence (Rhône & alentours) pour bien les
# CLASSER hors zone (anti-fuite par excès de prudence : on les rattache à leur dept).
_CITY_HINTS = {
    "ecully": "69", "tassin": "69", "lyon": "69", "dardilly": "69",
    "limonest": "69", "oullins": "69", "vourles": "69", "francheville": "69",
    "caluire": "69", "sainte foy": "69", "mont d or": "69", "beaujolais": "69",
    "monts du lyonnais": "69", "pierre benite": "69", "salvagny": "69",
    "lisieux": "14", "fervaques": "14", "dommartin": "01",
    "le chambon sur lignon": "43",
}


def _resolve_dept(text: str) -> str:
    """Déduit le code département d'un libellé textuel (titre). Retourne '' si
    indéterminable. On teste : noms de département entiers (DEPT_NOMS) puis indices
    de villes connues. Tout ce qui n'est pas résolu reste '' → exclu (0 fuite)."""
    n = _norm(text)
    # 1. Nom de département explicite dans le texte (le plus long d'abord pour éviter
    #    "Loire" qui matcherait dans "Loire-Atlantique").
    for name in sorted(_NAME_TO_CODE, key=len, reverse=True):
        if len(name) < 4:
            continue
        if f" {name} " in n:
            return _NAME_TO_CODE[name]
    # 2. Villes/secteurs connus
    for city, code in _CITY_HINTS.items():
        if f" {city} " in n:
            return code
    return ""


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)
    if not departements:
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        candidats: list[dict] = []
        for cat in LIST_CATEGORIES:
            for page in range(1, MAX_PAGES + 1):
                url = f"{BASE_URL}/{cat}-w{page}"
                try:
                    r = await client.get(url)
                except Exception as e:
                    print(f"[Dufosse] Erreur {cat} page {page}: {e}")
                    break
                if r.status_code != 200:
                    break
                cards = BeautifulSoup(r.text, "html.parser").select("div.ann")
                if not cards:
                    break

                new_on_page = 0
                for card in cards:
                    bien = _parse_card(card, cat)
                    if not bien:
                        continue
                    if bien["id_annonce"] in seen_ids:
                        continue
                    # Résolution département (anti-fuite stricte)
                    dept = _resolve_dept(bien["titre"])
                    if dept not in departements:
                        # hors zone OU indéterminable → exclu
                        seen_ids.add(bien["id_annonce"])
                        continue
                    bien["departement"] = dept
                    # bornes prix sur ce qu'on connaît
                    p = bien.get("prix") or 0
                    if prix_max and p and p > prix_max:
                        seen_ids.add(bien["id_annonce"])
                        continue
                    if prix_min and p and p < prix_min:
                        seen_ids.add(bien["id_annonce"])
                        continue
                    seen_ids.add(bien["id_annonce"])
                    candidats.append(bien)
                    new_on_page += 1

                if new_on_page == 0 and not cards:
                    break
                await asyncio.sleep(0.5)

        print(f"[Dufosse] {len(candidats)} annonces en zone cible (avant enrichissement)")

        # Enrichissement page détail (seulement les biens retenus)
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def enrich(b: dict):
            async with sem:
                try:
                    await _enrich_detail(client, b)
                except Exception as e:
                    print(f"[Dufosse] Erreur détail {b['id_annonce']}: {e}")
                await asyncio.sleep(0.3)

        await asyncio.gather(*(enrich(b) for b in candidats))

        # Filtre type + surface (après enrichissement)
        for b in candidats:
            blob = f"{b.get('type_bien') or ''} {b.get('titre') or ''}"
            if _EXCLUDE_TYPE.search(blob) and not _KEEP_TYPE.search(blob):
                continue
            if not _KEEP_TYPE.search(blob):
                continue
            s = b.get("surface") or 0
            if surface_min and s and s < surface_min:
                continue
            results.append(b)

    print(f"[Dufosse] {len(results)} maisons/propriétés retenues")
    return results


def _parse_card(card, cat: str) -> dict | None:
    a = card.select_one("h2.headline-ann a[href]")
    href = a.get("href", "") if a else ""
    if not href:
        return None
    url = _abs_url(href)

    m_id = re.search(r"-(\d+)$", href)
    id_path = m_id.group(1) if m_id else ""

    ref_el = card.select_one(".numerodemandat")
    ref = ""
    if ref_el:
        # "Ref. LE6132" → "LE6132"
        spans = ref_el.find_all("span")
        ref = spans[-1].get_text(strip=True) if spans else ""
    id_annonce = id_path or ref or url
    if not id_annonce:
        return None

    titre = a.get_text(" ", strip=True) or a.get("title", "")

    price_el = card.select_one(".prix")
    prix = parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    pieces = _pict_int(card, "nombredepieces")
    surface = _pict_surface(card, "superficie")
    if surface is None:
        surface = parse_surface(titre)

    photos = []
    img = card.select_one(".img--back img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            photos.append(_abs_url(src))

    type_bien = "propriété équestre" if "equestre" in cat else "maison"

    return {
        "source": "dufosse_immobilier",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": "",          # résolu dans search()
        "ville": _ville_from_titre(titre),
        "code_postal": None,        # jamais exposé par le site
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Dufossé Immobilier",
    }


async def _enrich_detail(client: httpx.AsyncClient, b: dict) -> None:
    r = await client.get(b["url"])
    if r.status_code != 200:
        return
    soup = BeautifulSoup(r.text, "html.parser")

    # Surface terrain / chambres via picts détail
    st = _pict_surface(soup, "surfaceterrain")
    if st is not None:
        b["surface_terrain"] = st
    ch = _pict_int(soup, "nombredechambres")
    if ch is not None:
        b["chambres"] = ch
    if b.get("surface") is None:
        b["surface"] = _pict_surface(soup, "superficie")
    if b.get("pieces") is None:
        b["pieces"] = _pict_int(soup, "nombredepieces")

    txt = soup.get_text(" ", strip=True)
    m = re.search(r"Note DPE\s+([A-G])\b", txt)
    if m:
        b["dpe"] = m.group(1)

    # Description (meta og:description, sinon texte de l'annonce)
    desc = ""
    og = soup.find("meta", property="og:description")
    if og and og.get("content"):
        desc = og["content"]
    b["description"] = desc[:1200]

    # Photos additionnelles
    photos = list(b.get("photos") or [])
    for img in soup.select(".img--back img, .gallery img, .slider img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:") and "interface" not in src:
            full = _abs_url(src)
            if full not in photos:
                photos.append(full)
    b["photos"] = photos[:PHOTOS_PER_CARD]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _pict_int(scope, klass: str) -> int | None:
    el = scope.select_one(f".pict.{klass} .font-weight-bold")
    if not el:
        return None
    return parse_int(r"(\d+)", el.get_text(" ", strip=True))


def _pict_surface(scope, klass: str) -> float | None:
    el = scope.select_one(f".pict.{klass} .font-weight-bold")
    if not el:
        return None
    raw = el.get_text(" ", strip=True)  # "450m²"
    val = re.sub(r"[^\d]", "", raw)
    try:
        return float(val) if val else None
    except ValueError:
        return None


def _ville_from_titre(titre: str) -> str:
    """Heuristique : 1er segment avant un séparateur (« Ecully, … », « Lot : … »)."""
    seg = re.split(r"[,:–\-]", titre, maxsplit=1)[0].strip()
    return seg[:80]


def _abs_url(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if not href.startswith("/"):
        href = "/" + href
    return BASE_URL + href


if __name__ == "__main__":
    standalone_main(search, "Dufosse")

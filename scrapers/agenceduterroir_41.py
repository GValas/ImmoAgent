"""scrapers/agenceduterroir_41.py — Agence du Terroir (Mondoubleau, Perche Vendômois)

Méthode : scrape_simple (httpx) — SSR HTML (PHP/Bootstrap, aucun JS requis).
Agence indépendante mono-secteur ancrée à 41170 Mondoubleau, spécialiste des
maisons de campagne / fermettes / longères du Perche Vendômois (Loir-et-Cher 41,
débordant sur le sud Sarthe 72 et le sud Eure-et-Loir 28 — tous départements cibles).

URL liste : /catalog.php?np={N}&ch_type=&ch_chambres=&ch_terrain=&ch_prix=&ch_ref=&ch_budget=
            (pagination par np ; ~12 cartes/page ; le filtre CH_TYPE=Maison existe
            aussi mais on garde tout puis on exclut terrains/locaux).

Cartes liste : div.flat-card
  - URL    : a[href] (page détail .html relative)
  - Ville  : .flat-card_title  → nom de commune (ex: "Romilly", "Berfay")
  - Prix   : .flat-card_price   → "530 000 €"
  - Réf    : "Réf. NNNN" dans .flat-card_descr
  - Extrait: .descri-complet (description longue déjà présente en liste)
  - Photo  : style background-image de la carte

Détail (enrichissement, 1 GET par bien retenu) : div.flat-card_info_item
  - "N ch."          → chambres
  - "Terrain : N m²"  → surface_terrain
  - galerie photos (style background-image ./photos/...)
  - titre h2 descriptif

⚠️ FILTRE DÉPARTEMENT — particularité : ni param dept serveur, ni code postal dans
la page (seul le CP de l'AGENCE 41170 apparaît, jamais celui du bien). On résout le
département à partir du NOM DE COMMUNE via geo.api.gouv.fr, **contraint aux
départements cibles**, doublé d'un dictionnaire d'overrides pour les communes
fusionnées/ambiguës du Perche (Arville/Saint-Agil/Souday = Couëtron-au-Perche 41,
Oigny 41, Le Gault-du-Perche 41…). Tout bien dont la commune ne se résout PAS de
façon fiable à un département cible est ÉCARTÉ → 0 fuite hors-zone garantie.
Le code postal renvoyé est celui de la commune résolue (geo API).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import unicodedata

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.agenceduterroir.com"
LIST_URL = (
    BASE_URL
    + "/catalog.php?np={np}&ch_type=&ch_chambres=&ch_terrain=&ch_prix=&ch_ref=&ch_budget="
)
MAX_PAGES = 12
PHOTOS_PER_CARD = 12

GEO_API = "https://geo.api.gouv.fr/communes"


# Départements cibles (zone Val-de-Loire / Ouest)
TARGET_DEPTS = {"72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"}

# Communes fusionnées / ambiguës du Perche Vendômois → département cible certain.
# (Arville, Saint-Agil, Souday sont des communes déléguées de Couëtron-au-Perche 41 ;
#  geo.api les rate ou les confond avec des homonymes hors-zone — on tranche ici.)
DEPT_OVERRIDE: dict[str, str] = {
    "arville": "41",
    "saint agil": "41",
    "st agil": "41",
    "souday": "41",
    "couetron au perche": "41",
    "oigny": "41",
    "le gault du perche": "41",
    "le plessis dorin": "41",
    "rahart": "41",
    "baillou": "41",
}

# Types à exclure (l'agence vend surtout des maisons, mais le catalogue mêle terrains)
_EXCLUDE_URL = re.compile(
    r"terrain|local|commerce|garage|parking|immeuble|bureau|fonds", re.IGNORECASE
)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", " ", s.lower()).strip()


def _base_name(city: str) -> str:
    """Retire les suffixes 'Couëtron-au-Perche' / 'du Perche' / 'au Perche'."""
    n = _norm(city)
    n = re.sub(r"\bcouetron au perche\b|\bcouyotron au perche\b", "", n).strip()
    n = re.sub(r"\bau perche\b|\bdu perche\b", "", n).strip()
    return re.sub(r"\s+", " ", n).strip()


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    # On ne retient in fine que l'intersection avec les cibles connues du secteur
    actifs = departements & TARGET_DEPTS
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()
    page_ids_seen: set[str] = set()
    geo_cache: dict[str, tuple[str | None, str | None]] = {}

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for np in range(1, MAX_PAGES + 1):
            try:
                r = await client.get(LIST_URL.format(np=np))
            except Exception as e:
                print(f"[AgenceTerroir] Erreur page {np}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.flat-card")
            if not cards:
                break

            new_on_page = 0
            all_seen = True
            for card in cards:
                link = card.select_one("a[href]")
                href = link.get("href", "") if link else ""
                m_id = re.search(r"-(\d+)\.html$", href)
                cid = m_id.group(1) if m_id else href
                if cid not in page_ids_seen:
                    all_seen = False
                    page_ids_seen.add(cid)
                try:
                    bien = await _parse_card(
                        card, client, geo_cache, actifs, prix_max, prix_min,
                        surface_min, seen_ids,
                    )
                except Exception:
                    continue
                if bien:
                    results.append(bien)
                    new_on_page += 1

            print(f"[AgenceTerroir] Page {np}: {len(cards)} cartes, {new_on_page} retenues")
            # Le catalogue boucle après la dernière vraie page : si toutes les cartes
            # de la page ont déjà été vues, on s'arrête.
            if all_seen:
                break
            await asyncio.sleep(0.5)

    return results


async def _parse_card(
    card,
    client: httpx.AsyncClient,
    geo_cache: dict,
    actifs: set[str],
    prix_max: int,
    prix_min: int,
    surface_min: int,
    seen_ids: set[str],
) -> dict | None:
    link = card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href or _EXCLUDE_URL.search(href):
        return None
    url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

    # Ville (titre de carte = commune)
    title_el = card.select_one(".flat-card_title")
    ville_raw = title_el.get_text(" ", strip=True) if title_el else ""
    if not ville_raw:
        return None

    # Résolution département via nom de commune (contrainte cibles) — 0 fuite
    dept, code_postal = await _resolve_dept(ville_raw, client, geo_cache)
    if dept is None or dept not in actifs:
        return None

    # Réf (id_annonce) : "Réf. NNNN" sinon id du slug d'URL
    descr_el = card.select_one(".flat-card_descr")
    descr_txt = descr_el.get_text(" ", strip=True) if descr_el else ""
    m_ref = re.search(r"R[ée]f\.?\s*([A-Za-z0-9\-]+)", descr_txt)
    ref = m_ref.group(1) if m_ref else ""
    m_id = re.search(r"-(\d+)\.html$", href)
    id_annonce = ref or (m_id.group(1) if m_id else url)
    if id_annonce in seen_ids:
        return None

    # Prix
    price_el = card.select_one(".flat-card_price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")
    if prix_max and prix and prix > prix_max:
        return None
    if prix_min and prix and prix < prix_min:
        return None

    # Description longue (déjà en liste)
    desc_el = card.select_one(".descri-complet") or card.select_one(".descri-extrait")
    description = desc_el.get_text(" ", strip=True) if desc_el else descr_txt
    description = re.sub(r"\s*R[ée]f\.?\s*[A-Za-z0-9\-]+\s*$", "", description).strip()

    # Photo de carte
    photos: list[str] = []
    bg = card.select_one("[style*=background-image]")
    if bg:
        mb = re.search(r"url\(([^)]+)\)", bg.get("style", ""))
        if mb:
            photos.append(_abs_photo(mb.group(1).strip("'\" ")))

    chambres = surface_terrain = None
    surface = None

    # Enrichissement page détail (chambres, terrain, galerie, description complète)
    try:
        rd = await client.get(url)
        if rd.status_code == 200:
            dsoup = BeautifulSoup(rd.text, "html.parser")
            for it in dsoup.select(".flat-card_info_item"):
                t = it.get_text(" ", strip=True)
                mc = re.search(r"(\d+)\s*ch", t, re.IGNORECASE)
                if mc and chambres is None:
                    chambres = int(mc.group(1))
                mt = re.search(r"Terrain\s*:?\s*([\d\s\xa0]+)\s*m", t, re.IGNORECASE)
                if mt and surface_terrain is None:
                    surface_terrain = _to_float(mt.group(1))
            # description complète (souvent identique à la liste, mais on prend la + longue)
            dd = dsoup.select_one(".descri-complet")
            if dd:
                full = dd.get_text(" ", strip=True)
                full = re.sub(r"\s*R[ée]f\.?\s*[A-Za-z0-9\-]+\s*$", "", full).strip()
                if len(full) > len(description):
                    description = full
            # galerie : <img src="./photos/...">  + éventuels background-image
            for img in dsoup.select("img"):
                src = img.get("src") or img.get("data-src") or ""
                if "photos/" in src:
                    p = _abs_photo(src)
                    if p not in photos:
                        photos.append(p)
            for el in dsoup.select("[style*=background-image]"):
                ms = re.search(r"url\(([^)]+)\)", el.get("style", ""))
                if ms:
                    p = _abs_photo(ms.group(1).strip("'\" "))
                    if "/photos/" in p and p not in photos:
                        photos.append(p)
            # titre descriptif (h2)
            h2 = dsoup.find("h2")
            titre_detail = h2.get_text(" ", strip=True) if h2 else ""
        else:
            titre_detail = ""
    except Exception:
        titre_detail = ""

    await asyncio.sleep(0.4)

    # Surface habitable : tentative depuis la description (best-effort)
    surface = _parse_surface_hab(description)
    if surface_min and surface and surface < surface_min:
        return None

    titre = titre_detail or f"Maison {ville_raw}".strip()

    seen_ids.add(id_annonce)
    return {
        "source": "agenceduterroir_41",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": "maison",
        "description": description[:1200],
        "departement": dept,
        "ville": ville_raw[:80],
        "code_postal": code_postal or "",
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Agence du Terroir (Mondoubleau)",
    }


async def _resolve_dept(
    ville_raw: str, client: httpx.AsyncClient, cache: dict
) -> tuple[str | None, str | None]:
    """(département, code_postal) à partir du nom de commune, contraint aux cibles."""
    n = _norm(ville_raw)
    if n in cache:
        return cache[n]

    # Overrides communes fusionnées / ambiguës du Perche
    for k, dep in DEPT_OVERRIDE.items():
        if k in n:
            res = (dep, None)
            cache[n] = res
            return res

    q = _base_name(ville_raw)
    if not q:
        cache[n] = (None, None)
        return None, None

    try:
        r = await client.get(
            GEO_API,
            params={
                "nom": q,
                "fields": "codeDepartement,nom,codesPostaux",
                "boost": "population",
                "limit": 15,
            },
            timeout=15,
        )
        data = r.json()
    except Exception:
        cache[n] = (None, None)
        return None, None

    cand = [d for d in data if d.get("codeDepartement") in TARGET_DEPTS]
    pool = cand or data
    chosen = None
    # match exact de nom prioritaire
    for d in pool:
        if _norm(d.get("nom", "")) == q:
            chosen = d
            break
    if chosen is None and pool:
        chosen = pool[0]

    if chosen is None:
        cache[n] = (None, None)
        return None, None

    dep = chosen.get("codeDepartement")
    if dep not in TARGET_DEPTS:
        cache[n] = (None, None)
        return None, None
    cps = chosen.get("codesPostaux") or []
    cp = cps[0] if cps else None
    cache[n] = (dep, cp)
    return dep, cp


# ── Helpers ──────────────────────────────────────────────────────────────────

def _abs_photo(src: str) -> str:
    src = src.lstrip("./")
    if src.startswith("http"):
        return src
    if src.startswith("//"):
        return "https:" + src
    return f"{BASE_URL}/{src.lstrip('/')}"


def _to_float(text: str) -> float | None:
    val = re.sub(r"[\s\xa0]", "", text)
    try:
        return float(val) if val else None
    except ValueError:
        return None


def _parse_surface_hab(text: str) -> float | None:
    if not text:
        return None
    m = re.search(
        r"(\d[\d\s\xa0]*)\s*m[²2]\s*(?:hab|habitable|de surface)", text, re.IGNORECASE
    )
    if not m:
        # "surface habitable de NNN m²"
        m = re.search(r"habitable[^0-9]{0,12}(\d[\d\s\xa0]*)\s*m[²2]", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 2000:
                return f
        except ValueError:
            pass
    return None


# ── CLI standalone ────────────────────────────────────────────────────────────

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
    print(f"\nTotal Agence du Terroir: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}/{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b.get('chambres') or '?'}ch — {b['ville']}"
            f" — {len(b['photos'])} photos"
        )

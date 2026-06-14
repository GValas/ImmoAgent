"""scrapers/pierres_caractere.py — Pierres et Caractère (Loire-Atlantique / Vendée)

Site : https://www.pierres-caractere.fr — agence WordPress (Nantes 44 / Vendée 85),
       biens anciens & de caractère. Couverture 44/85 → 0 stock zone cible attendu.

Méthode : scrape_simple (httpx) — SSR HTML (thème WordPress pc2020).

URL pattern (PAS de filtre département serveur) :
  - Pages catégorie « biens anciens », une par secteur :
      /biens-anciens/immobilier-ancien-contemporain-a-nantes/
      /biens-anciens/immobilier-ancien-contemporain-en-vendee/
    (pagination /page/N/ — la page suivante renvoie 200 mais 0 carte → on s'arrête)
  → on crawle ces listes et on POST-FILTRE par département.

Particularité ANTI-FUITE : aucun code postal au niveau du bien (seul le CP de
l'agence apparaît, en pied de page). La localisation fiable est le champ « Secteur »
de la page détail (ex. « Loire-Atlantique - Nantes »). On en déduit le département
via le référentiel DEPT_NOMS ; si le département est hors zone cible OU indéterminé,
le bien est EXCLU → 0 fuite.

Cartes (liste) : article.list_biens_single
  - URL    : a.list_biens_single_link[href]  → /biens/{id}-{slug}/
  - Titre  : .list_biens_single_title
  - Infos  : .list_biens_single_infos_span  (type, surface, pièces)
  - Prix   : .list_biens_single_price

Enrichissement (page détail, sur les seuls biens en zone cible) :
  - Champs structurés dl.single_carac_mozaik_dl (dt label / dd valeur) :
    Type de bien / Surface / Nombre de pièces / Secteur (→ département) / DPE / GES.
  - DPE : dd.dpe div.single[data-letter]. Description : og:description. Photos.

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
    standalone_main,
)

BASE_URL = "https://www.pierres-caractere.fr"
LIST_PATHS = [
    "/biens-anciens/immobilier-ancien-contemporain-a-nantes/",
    "/biens-anciens/immobilier-ancien-contemporain-en-vendee/",
]
MAX_PAGES = 10
PHOTOS_PER_CARD = 10
DETAIL_CONCURRENCY = 4

_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|long[èe]re|manoir|ch[âa]teau|moulin|"
    r"demeure|domaine|mas|h[ôo]tel particulier|g[îi]te|caract[èe]re",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain\b|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|viager|loft|studio",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    s = (s or "").lower()
    for a, b in (("à", "a"), ("â", "a"), ("ä", "a"), ("é", "e"), ("è", "e"),
                 ("ê", "e"), ("ë", "e"), ("î", "i"), ("ï", "i"), ("ô", "o"),
                 ("ö", "o"), ("û", "u"), ("ü", "u"), ("ç", "c")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return f" {s.strip()} "


_NAME_TO_CODE: dict[str, str] = {
    _norm(nom).strip(): code for code, nom in DEPT_NOMS.items()
}


def _resolve_dept(secteur: str) -> str:
    """Déduit le code département depuis le champ « Secteur » (« Loire-Atlantique -
    Nantes »). '' si indéterminable → bien exclu (anti-fuite)."""
    n = _norm(secteur)
    for name in sorted(_NAME_TO_CODE, key=len, reverse=True):
        if len(name) < 4:
            continue
        if f" {name} " in n:
            return _NAME_TO_CODE[name]
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
        for path in LIST_PATHS:
            for page in range(1, MAX_PAGES + 1):
                url = BASE_URL + path
                if page > 1:
                    url = url.rstrip("/") + f"/page/{page}/"
                try:
                    r = await client.get(url)
                except Exception as e:
                    print(f"[PierresCaractere] Erreur {path} page {page}: {e}")
                    break
                if r.status_code != 200:
                    break
                cards = BeautifulSoup(r.text, "html.parser").select(
                    "article.list_biens_single"
                )
                if not cards:
                    break

                for card in cards:
                    bien = _parse_card(card)
                    if not bien or bien["id_annonce"] in seen_ids:
                        continue
                    seen_ids.add(bien["id_annonce"])
                    p = bien.get("prix") or 0
                    if prix_max and p and p > prix_max:
                        continue
                    if prix_min and p and p < prix_min:
                        continue
                    candidats.append(bien)

                await asyncio.sleep(0.5)

        print(f"[PierresCaractere] {len(candidats)} annonces brutes (avant enrichissement)")

        # Enrichissement détail (Secteur → département, surface, terrain, DPE)
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def enrich(b: dict):
            async with sem:
                try:
                    await _enrich_detail(client, b)
                except Exception as e:
                    print(f"[PierresCaractere] Erreur détail {b['id_annonce']}: {e}")
                await asyncio.sleep(0.3)

        await asyncio.gather(*(enrich(b) for b in candidats))

        # Post-filtre département (anti-fuite) + type + surface
        for b in candidats:
            dept = b.get("departement") or ""
            if dept not in departements:
                continue
            blob = f"{b.get('type_bien') or ''} {b.get('titre') or ''}"
            if _EXCLUDE_TYPE.search(blob) and not _KEEP_TYPE.search(blob):
                continue
            if not _KEEP_TYPE.search(blob):
                continue
            s = b.get("surface") or 0
            if surface_min and s and s < surface_min:
                continue
            results.append(b)

    print(f"[PierresCaractere] {len(results)} biens retenus en zone cible")
    return results


def _parse_card(card) -> dict | None:
    a = card.select_one("a.list_biens_single_link[href]")
    href = a.get("href", "") if a else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    m_id = re.search(r"/biens/(\d+)", url)
    id_annonce = m_id.group(1) if m_id else url
    if not id_annonce:
        return None

    title_el = card.select_one(".list_biens_single_title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    spans = [s.get_text(" ", strip=True)
             for s in card.select(".list_biens_single_infos_span")]
    infos = " ".join(spans)
    type_bien = spans[0] if spans else ""
    surface = _surface(infos)
    pieces = parse_int(r"(\d+)\s*pi[èe]ce", infos)

    price_el = card.select_one(".list_biens_single_price")
    prix = parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    photos = []
    style = card.get("style", "") or ""
    m_bg = re.search(r"url\(['\"]?(https?://[^'\")]+)", style)
    if m_bg:
        photos.append(m_bg.group(1))

    return {
        "source": "pierres_caractere",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": "",          # résolu à l'enrichissement (Secteur)
        "ville": "",
        "code_postal": None,        # jamais exposé par le site
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Pierres et Caractère",
    }


async def _enrich_detail(client: httpx.AsyncClient, b: dict) -> None:
    r = await client.get(b["url"])
    if r.status_code != 200:
        return
    soup = BeautifulSoup(r.text, "html.parser")

    # Champs structurés dt/dd
    for dl in soup.select("dl.single_carac_mozaik_dl"):
        dt = dl.select_one(".single_carac_mozaik_dt")
        dd = dl.select_one(".single_carac_mozaik_dd")
        if not dt or not dd:
            continue
        label = _norm(dt.get_text(" ", strip=True))
        value = dd.get_text(" ", strip=True)
        if "secteur" in label:
            dept = _resolve_dept(value)
            if dept:
                b["departement"] = dept
            # ville = dernier segment après le tiret
            parts = re.split(r"[-–]", value)
            if len(parts) > 1:
                b["ville"] = parts[-1].strip()[:80]
        elif "surface terrain" in label:
            b["surface_terrain"] = _surface(value)
        elif "surface" in label and b.get("surface") is None:
            b["surface"] = _surface(value)
        elif "piece" in label and b.get("pieces") is None:
            b["pieces"] = parse_int(r"(\d+)", value)
        elif "chambre" in label:
            b["chambres"] = parse_int(r"(\d+)", value)
        elif "type de bien" in label and value:
            b["type_bien"] = value

    # DPE (dd.dpe div.single[data-letter])
    dpe_el = soup.select_one("dd.dpe .single[data-letter], .single_carac_mozaik_dd.dpe .single[data-letter]")
    if dpe_el and dpe_el.get("data-letter"):
        letter = dpe_el["data-letter"].strip().upper()
        if letter in "ABCDEFG":
            b["dpe"] = letter

    # Description
    desc = ""
    og = soup.find("meta", property="og:description")
    if og and og.get("content"):
        desc = og["content"]
    b["description"] = desc[:1200]

    # Photos
    photos = list(b.get("photos") or [])
    for img in soup.select(".single_gallery img, .gallery img, img[class*=bien]"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            full = src if src.startswith("http") else BASE_URL + src
            if full not in photos:
                photos.append(full)
    b["photos"] = photos[:PHOTOS_PER_CARD]


def _surface(text: str) -> float | None:
    """'210 m²' / '210m²' / '69.8 m²' → float."""
    m = re.search(r"(\d[\d\s.,]*)\s*m", text or "")
    if not m:
        return None
    val = m.group(1).replace(" ", "").replace("\xa0", "").replace(",", ".")
    val = val.rstrip(".")
    try:
        return float(val)
    except ValueError:
        return None


if __name__ == "__main__":
    standalone_main(search, "PierresCaractere")

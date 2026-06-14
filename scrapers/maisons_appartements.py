"""scrapers/maisons_appartements.py — Maisons & Appartements (portail national Poliris)

Site : https://www.maisonsetappartements.fr — gros portail/annuaire national
       d'agences (moteur Poliris). Le « hub » de département agrège des
       micro-listings d'agences par ville.

Méthode : scrape_simple (httpx) — SSR HTML pur (cartes article.RR_article avec
          JSON-LD complet : prix, surface, pièces, ville, image — vérifié 200).

Énumération terminable (PAS de listing national paginable, mais un chemin propre) :
  1. Hub département : /fr/maison-appartement-{slug}-{NN}.html
     → liste les pages de « sélection de biens » d'agences du département.
  2. Page sélection : /fr/{NN}/maisons/vente/selection-biens-{ville}-{id}.html
     → cartes article.RR_article (1 à quelques biens chacune).
  On exclut les sélections « immobilier-neuf » (programmes neufs, hors cible).

Carte (article.RR_article) :
  - JSON-LD (script[type=application/ld+json], parse strict=False car retours à la
    ligne bruts) : name ("Maison à vendre - 5 pièces - 185 m²"), url
    (/fr/{NN}/annonce-vente-{type}-{ville}-{id}.html → dept + ville + id + type),
    floorSize, numberOfRooms, address (ville), image, description.
  - Prix : .RR_prix ("1 970 000 €").

Le code postal à 5 chiffres n'est PAS exposé en liste ; le département vient du
segment d'URL /fr/{NN}/. POST-FILTRE strict : on ne retient un bien que si ce NN
est dans les départements cibles (0 fuite). On renseigne `code_postal` = NN (le
filtre code_postal[:2] reste cohérent).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import DEFAULT_DEPT_SLUGS, HEADERS, parse_price

BASE_URL = "https://www.maisonsetappartements.fr"
HUB_URL = BASE_URL + "/fr/maison-appartement-{slug}-{dept}.html"
SELECTION_CONCURRENCY = 4
PHOTOS_PER_CARD = 10

# Types de bien (segment d'URL annonce-vente-{type}) à conserver.
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|hotel-particulier",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"viager|programme",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
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
        # 1. Hub par département → URLs des pages de sélection (hors immobilier-neuf)
        selection_urls: list[tuple[str, str]] = []   # (dept, url)
        for dept in departements:
            slug = DEFAULT_DEPT_SLUGS.get(dept)
            if not slug:
                continue
            try:
                r = await client.get(HUB_URL.format(slug=slug, dept=dept))
            except Exception as e:
                print(f"[MaisonsAppart] Erreur hub {dept}: {e}")
                continue
            if r.status_code != 200:
                continue
            for sel in _extract_selection_links(r.text, dept):
                selection_urls.append((dept, sel))
            await asyncio.sleep(0.5)

        print(f"[MaisonsAppart] {len(selection_urls)} pages de sélection à scraper")

        # 2. Pages de sélection → cartes article.RR_article
        sem = asyncio.Semaphore(SELECTION_CONCURRENCY)

        async def scrape_selection(dept: str, url: str) -> list[dict]:
            async with sem:
                try:
                    r = await client.get(url)
                except Exception as e:
                    print(f"[MaisonsAppart] Erreur sélection {url}: {e}")
                    return []
                await asyncio.sleep(0.5)
                if r.status_code != 200:
                    return []
                out: list[dict] = []
                soup = BeautifulSoup(r.text, "html.parser")
                for card in soup.select("article.RR_article"):
                    bien = _parse_card(card)
                    if bien:
                        out.append(bien)
                return out

        batches = await asyncio.gather(
            *(scrape_selection(d, u) for d, u in selection_urls)
        )

        for batch in batches:
            for bien in batch:
                cp = bien["code_postal"]
                if not cp or cp[:2] not in departements:
                    continue
                if bien["id_annonce"] in seen_ids:
                    continue
                p = bien.get("prix") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                s = bien.get("surface") or 0
                if surface_min and s and s < surface_min:
                    continue
                seen_ids.add(bien["id_annonce"])
                results.append(bien)

    print(f"[MaisonsAppart] {len(results)} maisons/propriétés retenues en zone cible")
    return results


def _extract_selection_links(html: str, dept: str) -> list[str]:
    """Liens de sélection de biens du département (hors programmes neufs)."""
    pat = rf'href="({re.escape(BASE_URL)}/fr/{dept}/[^"]*selection-biens[^"]*)"'
    links = set(re.findall(pat, html))
    return sorted(s for s in links if "immobilier-neuf" not in s)


def _parse_card(card) -> dict | None:
    ld_el = card.select_one('script[type="application/ld+json"]')
    if not ld_el or not ld_el.string:
        return None
    try:
        ld = json.loads(ld_el.string, strict=False)
    except Exception:
        return None
    if not isinstance(ld, dict):
        return None

    url = (ld.get("url") or "").strip()
    if not url:
        return None

    # Département + type + id depuis l'URL : /fr/{NN}/annonce-vente-{type}-{ville}-{id}.html
    m_dep = re.search(r"/fr/(\d{2,3})/", url)
    dept = m_dep.group(1).zfill(2) if m_dep else ""
    m_type = re.search(r"annonce-vente-([a-zàâäéèêëîïôöùûüç]+)-", url, re.IGNORECASE)
    type_seg = m_type.group(1) if m_type else ""
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if type_seg and not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = (type_seg or "maison").replace("-", " ")

    m_id = re.search(r"-(\d+)\.html", url)
    id_annonce = m_id.group(1) if m_id else url

    name = _unescape(ld.get("name") or "")
    alt = _unescape(ld.get("alternateName") or "")
    titre = alt or name

    description = _unescape(ld.get("description") or "")

    # Ville : champ address (souvent juste le nom de commune)
    addr = ld.get("address")
    ville = ""
    if isinstance(addr, str):
        ville = addr
    elif isinstance(addr, dict):
        ville = addr.get("addressLocality") or addr.get("name") or ""
    ville = _unescape(ville).title()

    # Surface / pièces : champs JSON-LD, secours sur le name ("- 5 pièces - 185 m²")
    surface = _to_float(ld.get("floorSize"))
    if surface is None:
        m = re.search(r"(\d[\d\s]*)\s*m", name)
        if m:
            surface = _to_float(m.group(1))
    pieces = _to_int(ld.get("numberOfRooms"))
    if pieces is None:
        m = re.search(r"(\d+)\s*pi[èe]ce", name, re.IGNORECASE)
        if m:
            pieces = int(m.group(1))

    price_el = card.select_one(".RR_prix")
    prix = parse_price(price_el.get_text(" ", strip=True)) if price_el else None

    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    photos = []
    img = ld.get("image")
    if isinstance(img, str) and img:
        photos.append(img)
    elif isinstance(img, list):
        photos.extend(str(x) for x in img if x)
    for tag in card.select("img"):
        src = tag.get("data-src") or tag.get("src") or ""
        if src and src.startswith("http") and src not in photos:
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "maisons_appartements",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": dept,          # CP 5 chiffres absent en liste → dept (2 chiffres)
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": None,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _unescape(s: str) -> str:
    """Décode les entités HTML résiduelles (&agrave;, &sup2;…)."""
    import html as _html
    return _html.unescape(s or "").strip()


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(re.sub(r"[^\d.]", "", str(v)) or "0") or None
    except ValueError:
        return None


def _to_int(v) -> int | None:
    if v is None:
        return None
    m = re.search(r"\d+", str(v))
    return int(m.group()) if m else None


if __name__ == "__main__":
    from scrapers._base import standalone_main

    standalone_main(search, "Maisons & Appartements")

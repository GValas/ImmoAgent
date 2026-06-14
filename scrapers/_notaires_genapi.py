"""scrapers/_notaires_genapi.py — Socle partagé pour les offices notariaux servis par
le gabarit « immobilier.notaires.fr / Genapi » (div.bloc-annonce).

Plusieurs offices notariaux (ex. Château-Gontier 53) publient leurs annonces via le
même gabarit Genapi : page /annonces-immobilieres.html, cartes `div.bloc-annonce`,
ville + code département dans `.titre` (« CHATEAU GONTIER (53) »), type/pièces/surface/
prix dans `.titre-detail`, lien détail vers immobilier.notaires.fr (le code dept y
figure aussi). Ce module factorise le parsing ; chaque office n'a plus qu'à fournir son
`base_url`, son `source`, son `label` et son `agence`.

Filtre DÉPARTEMENT : code extrait des parenthèses de `.titre` ET re-confirmé par l'URL
détail → POST-FILTRE STRICT sur la zone cible → 0 fuite hors-zone garantie.
"""
from __future__ import annotations

import re

from scrapers._base import get_with_retry, make_client, parse_int

LISTING_PATH = "/annonces-immobilieres.html"

_KEEP_TYPE = re.compile(
    r"maison|villa|propri[ée]t[ée]|ferme|long[èe]re|manoir|chateau|ch[âa]teau|"
    r"moulin|demeure|domaine|mas|corps[- ]de[- ]ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|garage|parking|immeuble|local|commerce|bureau|"
    r"fonds|cave|box|studio|murs|d[ée]p[ôo]t",
    re.IGNORECASE,
)


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text or "")
    try:
        val = float(cleaned) if cleaned else None
        return val if (val and val >= 1000) else None
    except ValueError:
        return None


def _parse_card(card, base_url: str, source: str, agence: str) -> dict | None:
    titre_el = card.select_one(".titre")
    titre_txt = titre_el.get_text(" ", strip=True) if titre_el else ""
    # Ville + code département : « CHATEAU GONTIER (53) ».
    m_dep = re.search(r"\((\d{2,3})\)", titre_txt)
    dept = m_dep.group(1)[:2] if m_dep else ""
    ville = re.sub(r"\s*\(\d{2,3}\)\s*$", "", titre_txt).strip().title()

    detail_el = card.select_one(".titre-detail")
    detail_txt = detail_el.get_text(" ", strip=True) if detail_el else ""
    detail_txt = " ".join(detail_txt.split())

    # Type de bien (avant le premier «-»).
    type_part = detail_txt.split(" - ", 1)[0].strip() or detail_txt[:40]
    if _EXCLUDE_TYPE.search(type_part) and not _KEEP_TYPE.search(type_part):
        return None
    if not _KEEP_TYPE.search(type_part):
        return None
    type_bien = type_part.lower()

    # Lien détail (immobilier.notaires.fr) → id + re-confirme le dept (…-{ville}-{dep}/{id}).
    link = card.find("a", href=re.compile(r"immobilier\.notaires\.fr"))
    detail_url = link.get("href", "") if link else ""
    m_url = re.search(r"-(\d{2,3})/(\d+)\b", detail_url)
    if m_url:
        url_dept = m_url.group(1)[:2]
        if not dept:
            dept = url_dept
        # Si les deux divergent, on fait confiance au CP de l'URL détail (autorité).
        elif url_dept and url_dept != dept:
            dept = url_dept
    if not dept:
        return None

    # id_annonce : numéro de l'URL détail, sinon lien « En savoir plus » local.
    id_annonce = ""
    if m_url:
        id_annonce = m_url.group(2)
    if not id_annonce:
        local = card.find("a", href=re.compile(r"/(\d+)\.html"))
        if local:
            mm = re.search(r"/(\d+)\.html", local.get("href", ""))
            id_annonce = mm.group(1) if mm else ""
    url = detail_url or (
        (base_url + local.get("href")) if local else base_url + LISTING_PATH
    )
    if not id_annonce:
        id_annonce = url

    pieces = parse_int(r"(\d+)\s*pi[èe]ce", detail_txt)
    surface = None
    m_s = re.search(r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m[²2]\b", detail_txt)
    if m_s:
        try:
            surface = float(re.sub(r"[\s\xa0]", "", m_s.group(1)).replace(",", "."))
            if not (8 <= surface <= 5000):
                surface = None
        except ValueError:
            surface = None

    prix = None
    m_pr = re.search(r"([\d][\d\s\xa0]{2,})\s*€", detail_txt)
    if m_pr:
        prix = _parse_price(m_pr.group(1))

    desc_el = card.select_one(".desc-immo-detail")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""
    description = " ".join(description.split())

    photos: list[str] = []
    img = card.find("img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:") and "marianne" not in src:
            photos.append(src if src.startswith("http") else base_url + src)

    titre = f"{type_bien.title()} à {ville}".strip() if ville else type_bien.title()

    return {
        "source": source,
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien[:60],
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",  # CP exact récupéré en page détail (gallery.py)
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos[:1],
        "dpe": None,
        "agence": agence,
    }


async def run_office_search(
    *, base_url: str, source: str, label: str, agence: str, criteres: dict,
) -> list[dict]:
    from bs4 import BeautifulSoup

    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen: set[str] = set()

    async with make_client(timeout=25) as client:
        r = await get_with_retry(client, base_url + LISTING_PATH)
        if r is None or r.status_code != 200:
            print(f"[{label}] Listing inaccessible (status "
                  f"{getattr(r, 'status_code', 'None')})")
            return results

        cards = BeautifulSoup(r.text, "html.parser").select(".bloc-annonce")
        for card in cards:
            try:
                bien = _parse_card(card, base_url, source, agence)
            except Exception:
                continue
            if not bien:
                continue
            if bien["departement"] not in departements:
                continue
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
            results.append(bien)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print(f"[{label}] total: {len(results)} biens (zone cible) — par dept: {by_dept}")
    return results

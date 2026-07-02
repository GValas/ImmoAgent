"""scrapers/ordim_immo.py — Groupe ORDIM (réseau d'agences Yonne/Nièvre/Loiret)

Site : https://www.ordim-immo.com — réseau d'agences indépendantes en
       Bourgogne / Centre (Yonne 89, Nièvre 58, Loiret 45, et marges 10/18...).

Méthode : scrape_simple (httpx) — SSR HTML, moteur Poliris/Wizzim.

URL pattern :
  - Page globale du réseau, PAS de filtre département serveur :
      /annonces/transaction/vente.html                       (page 1)
      /annonces/transaction_____{N}/vente.html                (page N, 6/page)
  → on scrape toutes les pages et on POST-FILTRE sur code_postal[:2].

Cartes (liste) : div.listing-item
  - URL    : a.link-product[href]  → ../fiches/{code}_{id}/{slug}.html
  - Nom    : .product-name
  - Loc    : .product-localisation  →  "BLENEAU (89220)"
  - Infos  : .product-short-infos   →  "12 pièce(s) /  373 m²"
  - Prix   : .product-price         →  "399 000 €"
  - Réf    : .product-ref           →  "Ref : 14711"
  - Photo  : .product-image img[src] (chemin relatif ../office24/...)

Enrichissement (page détail, seulement sur les biens du département cible) :
  - Type de bien / Surface terrain / Chambres / Classe énergie (DPE)
    extraits du texte de la fiche. On ne garde que maisons / propriétés.

Filtre département : POST-FILTRE strict code_postal[:2] ∈ departements
(le réseau déborde sur 10/18/58 hors zone → filtrage indispensable, 0 fuite).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.ordim-immo.com"
MAX_PAGES = 60          # ~55 pages observées (327 annonces, 6/page)
PHOTOS_PER_CARD = 10
DETAIL_CONCURRENCY = 4  # enrichissement détail poli


# Types de bien (page détail) à conserver
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|long[èe]re|manoir|ch[âa]teau|moulin|"
    r"demeure|domaine|mas|corps de ferme|maison de village|g[îi]te",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|viager|loft|chambre|box|cave",
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
        # 1. Scrape toutes les pages de liste, post-filtre département
        candidats: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            if page == 1:
                url = f"{BASE_URL}/annonces/transaction/vente.html"
            else:
                url = f"{BASE_URL}/annonces/transaction_____{page}/vente.html"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Ordim] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.listing-item")
            if not cards:
                break

            for card in cards:
                bien = _parse_card(card)
                if not bien:
                    continue
                cp = bien["code_postal"]
                if not cp or cp[:2] not in departements:
                    continue
                if bien["id_annonce"] in seen_ids:
                    continue
                # bornes prix sur ce qu'on connaît déjà
                p = bien.get("prix") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                bien["departement"] = cp[:2]
                seen_ids.add(bien["id_annonce"])
                candidats.append(bien)

            await asyncio.sleep(0.5)

        print(f"[Ordim] {len(candidats)} annonces dans la zone (avant enrichissement)")

        # 2. Enrichissement page détail (type / terrain / chambres / dpe)
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def enrich(b: dict):
            async with sem:
                try:
                    await _enrich_detail(client, b)
                except Exception as e:
                    print(f"[Ordim] Erreur détail {b['id_annonce']}: {e}")
                await asyncio.sleep(0.3)

        await asyncio.gather(*(enrich(b) for b in candidats))

        # 3. Filtre type + surface (après enrichissement)
        for b in candidats:
            type_clean = b.get("type_bien") or ""
            if _EXCLUDE_TYPE.search(type_clean) and not _KEEP_TYPE.search(type_clean):
                continue
            if type_clean and not _KEEP_TYPE.search(type_clean):
                continue
            s = b.get("surface") or 0
            if surface_min and s and s < surface_min:
                continue
            results.append(b)

    print(f"[Ordim] {len(results)} maisons/propriétés retenues")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a.link-product[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = _abs_url(href)

    # id_annonce : ref affichée, sinon id du chemin ../fiches/{code}_{id}/...
    ref_el = card.select_one(".product-ref")
    ref = ""
    if ref_el:
        m = re.search(r"(\d+)", ref_el.get_text(" ", strip=True))
        if m:
            ref = m.group(1)
    id_path = ""
    m_id = re.search(r"_(\d+)/", href)
    if m_id:
        id_path = m_id.group(1)
    id_annonce = ref or id_path or url
    if not id_annonce:
        return None

    name_el = card.select_one(".product-name")
    titre = name_el.get_text(" ", strip=True) if name_el else ""

    loc_el = card.select_one(".product-localisation")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)

    price_el = card.select_one(".product-price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    infos_el = card.select_one(".product-short-infos")
    infos = infos_el.get_text(" ", strip=True) if infos_el else ""
    pieces = _parse_int(r"(\d+)\s*pi[èe]ce", infos)
    surface = _parse_float(r"([\d\s]+)\s*m", infos)

    if not titre:
        titre = f"{ville}".strip()

    # Photos (liste) — chemin relatif → absolu
    photos = []
    img = card.select_one(".product-image img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and "pr_p" in src:
            photos.append(_abs_url(src))

    return {
        "source": "ordim_immo",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": None,           # rempli à l'enrichissement
        "description": "",
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Groupe ORDIM",
    }


async def _enrich_detail(client: httpx.AsyncClient, b: dict) -> None:
    r = await client.get(b["url"])
    if r.status_code != 200:
        return
    soup = BeautifulSoup(r.text, "html.parser")
    txt = soup.get_text(" ", strip=True)

    # Type de bien : préférer le breadcrumb (libellé propre : "Maison"),
    # secours sur "Type de bien <X>" (capture courte, 1 mot).
    type_bien = None
    bc = soup.select_one(".breadcrumb")
    if bc:
        crumbs = [a.get_text(strip=True) for a in bc.select("a")]
        for c in crumbs:
            if _KEEP_TYPE.search(c) or _EXCLUDE_TYPE.search(c):
                type_bien = c.rstrip("s")  # "Maisons" → "Maison"
                break
    if not type_bien:
        m = re.search(r"Type de bien\s+([A-Za-zÀ-ÿ'-]{3,20})", txt)
        if m:
            type_bien = m.group(1).strip()
    b["type_bien"] = (type_bien or "maison").strip()

    # Surface terrain
    m = re.search(r"Surface terrain\s+([\d\s]+)\s*m", txt, re.IGNORECASE)
    if m:
        b["surface_terrain"] = _to_float(m.group(1))

    # Chambres
    m = re.search(r"Chambres?\s+(\d+)", txt, re.IGNORECASE)
    if m:
        b["chambres"] = int(m.group(1))

    # Surface habitable (secours si absente de la liste)
    if not b.get("surface"):
        m = re.search(r"Surface habitable\s+([\d\s]+)\s*m", txt, re.IGNORECASE)
        if m:
            b["surface"] = _to_float(m.group(1))

    # DPE — classe énergie
    m = re.search(r"Classe énergie\s+([A-G])\b", txt)
    if m:
        b["dpe"] = m.group(1)

    # Description
    desc_el = soup.select_one(".product-description, .description, .product-text")
    if desc_el:
        b["description"] = desc_el.get_text(" ", strip=True)[:1200]

    # Photos additionnelles
    photos = list(b.get("photos") or [])
    for img in soup.select("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and "pr_p" in src:
            full = _abs_url(src)
            if full not in photos:
                photos.append(full)
    b["photos"] = photos[:PHOTOS_PER_CARD]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _abs_url(href: str) -> str:
    if href.startswith("http"):
        return href
    href = href.lstrip(".")          # "../fiches/..." → "/fiches/..."
    if not href.startswith("/"):
        href = "/" + href
    return BASE_URL + href


def _parse_loc(text: str) -> tuple[str, str]:
    """'BLENEAU (89220)' → ('Bleneau', '89220')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip().title()
    return ville, cp


def _parse_float(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return _to_float(m.group(1)) if m else None


def _to_float(s: str) -> float | None:
    val = re.sub(r"[\s\xa0]", "", s)
    try:
        return float(val) if val else None
    except ValueError:
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
    print(f"\nTotal Ordim: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']} — DPE {b.get('dpe')}"
        )

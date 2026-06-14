"""scrapers/delta_immobilier.py — Delta Immobilier (Mehun-sur-Yèvre, Cher 18)

Méthode : scrape_simple (httpx) — SSR HTML (appli Tailwind, rendu serveur).
URL liste : /acheter   (catalogue complet sur une page, ~39 biens, pas de
            pagination ; pas de filtre dept côté serveur → POST-FILTRE STRICT
            sur code_postal[:2]).
Cartes : div.rounded-lg contenant a[href^='/bien/']
  - a[href='/bien/{id}']        → URL détail + id_annonce
  - img[alt]                    → titre de secours
  - h3                          → titre
  - bloc localisation           → "MEHUN SUR YEVRE (18500)"  (ville + CODE POSTAL)
  - bloc stats                  → "7 pcs 3 1 128 m²" (pièces, chambres, sdb, surface)
  - "DPE X"                     → classe DPE
  - .text-primary (price)       → "140 000 €"

Agence locale couvrant Mehun-sur-Yèvre / Bourges / Vierzon → tout l'inventaire est
dans le Cher (18). CP présent dans chaque carte → post-filtre fiable, 0 fuite.
Types non résidentiels (appartement/terrain/local…) écartés.

Interface : async def search(criteres: dict) -> list[dict]
"""
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_price

BASE_URL = "https://www.deltaimmobilier-mehun.com"
LIST_URL = f"{BASE_URL}/acheter"

_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|terrain|local|commerce|garage|parking|immeuble|"
    r"bureau|fonds|viager|box|duplex\b",
    re.IGNORECASE,
)
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|long[èe]re|manoir|ch[âa]teau|moulin|demeure|"
    r"domaine|ferme|fermette|corps de ferme|pavillon|grange|gentilhommi",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen: set[str] = set()

    async with make_client(timeout=25) as client:
        r = await get_with_retry(client, LIST_URL)
        if r is None or r.status_code != 200:
            print(f"[Delta] Liste indisponible (status {getattr(r, 'status_code', '?')})")
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        cards: list = []
        for a in soup.select("a[href^='/bien/']"):
            card = a.find_parent("div", class_=re.compile("rounded-lg"))
            if card is not None and card not in cards:
                cards.append(card)

        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue
            cp = str(bien.get("code_postal") or "")
            if not cp or cp[:2] not in departements:
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

    print(f"[Delta] {len(results)} annonces dans la zone")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a[href^='/bien/']")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href
    m_id = re.search(r"/bien/(\d+)", href)
    id_annonce = m_id.group(1) if m_id else url

    h3 = card.select_one("h3")
    titre = h3.get_text(" ", strip=True) if h3 else ""
    img = card.select_one("img[alt]")
    if not titre and img:
        titre = img.get("alt", "")
    titre = re.sub(r"\s+", " ", titre).strip()

    full = card.get_text(" ", strip=True)

    # exclusion type (titre)
    if _EXCLUDE_TYPE.search(titre) and not _KEEP_TYPE.search(titre):
        return None
    type_bien = "maison"
    mt = _KEEP_TYPE.search(titre)
    if mt:
        type_bien = mt.group(0).lower()

    # localisation "MEHUN SUR YEVRE (18500)"
    ville = ""
    code_postal = ""
    mloc = re.search(r"([A-ZÀ-Ÿ][A-Za-zÀ-ÿ '\-]+?)\s*\((\d{5})\)", full)
    if mloc:
        ville = mloc.group(1).strip().title()
        code_postal = mloc.group(2)

    # stats : bloc dédié "7 pcs 3 1 128 m²" (surface HABITABLE, distincte du
    # terrain mentionné dans le titre type "jardin de 1914m²").
    pieces = chambres = surface = None
    stats = None
    # bloc stats = div à bordure haut/bas, libellé court "N pcs … NNN m²"
    stats_el = card.select_one("div.border-t.border-b")
    if stats_el is None:
        # repli : le plus PETIT div contenant "pcs" et "m²"
        cands = [
            el for el in card.find_all("div")
            if "pcs" in el.get_text(" ", strip=True).lower()
            and "m²" in el.get_text(" ", strip=True)
        ]
        if cands:
            stats_el = min(cands, key=lambda e: len(e.get_text(strip=True)))
    if stats_el is not None:
        # enfants directs : ["7 pcs", "3" (chambres), "1" (sdb), "128 m²"]
        parts = [c.get_text(" ", strip=True) for c in stats_el.find_all(recursive=False)]
        if not parts:
            parts = re.split(r"\s{2,}", stats_el.get_text("  ", strip=True))
        for part in parts:
            mp = re.search(r"(\d+)\s*pcs", part, re.IGNORECASE)
            if mp:
                pieces = int(mp.group(1))
                continue
            ms = re.search(r"(\d[\d\s]*)\s*m²", part)
            if ms:
                try:
                    surface = float(re.sub(r"\s", "", ms.group(1)))
                except ValueError:
                    surface = None
                continue
        # chambres = 1er enfant purement numérique après le bloc "pcs"
        nums = [p.strip() for p in parts if re.fullmatch(r"\d+", p.strip())]
        if nums:
            chambres = int(nums[0])

    dpe = None
    mdpe = re.search(r"DPE\s*([A-G])\b", full, re.IGNORECASE)
    if mdpe:
        dpe = mdpe.group(1).upper()

    # prix
    price_el = card.select_one(".text-primary") or card.select_one("[class*='font-bold']")
    prix = parse_price(price_el.get_text(" ", strip=True) if price_el else "")
    if prix is None:
        mpr = re.search(r"([\d\s]{4,})\s*€", full)
        if mpr:
            prix = parse_price(mpr.group(1))

    photos = []
    if img:
        src = img.get("src") or ""
        if src and not src.startswith("data:"):
            photos.append(src if src.startswith("http") else BASE_URL + src)

    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    return {
        "source": "delta_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2] if code_postal else None,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Delta Immobilier",
    }


if __name__ == "__main__":
    import asyncio
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
    print(f"\nTotal Delta: {len(biens)} annonces")
    depts = sorted({str(b.get("code_postal") or "")[:2] for b in biens if b.get("code_postal")})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(f"  [{b.get('code_postal')}] {str(b.get('titre'))[:50]} — "
              f"{b.get('prix')}€ — {b.get('surface') or '?'}m² — DPE {b.get('dpe')} — {b.get('ville')}")

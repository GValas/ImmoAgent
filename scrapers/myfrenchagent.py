"""scrapers/myfrenchagent.py — My French Agent (réseau national de mandataires)

Méthode : scrape_simple (httpx) — SSR HTML (Bootstrap, pas de JS requis)
URL liste : /nos-biens/1-acheter?idPage={N}&typeBien=1   (typeBien=1 = maison)
            → PAS de filtre département dans l'URL (le param `location` ne fait
            que TRIER, il ne filtre pas). On pagine la liste nationale des
            maisons (~12 biens/page, ~7 pages → ~80 biens), puis on POST-FILTRE
            strictement sur le code postal récupéré en page détail.

Cartes liste : div.card (lien a[href*="/nos-biens/1-acheter/"])
  - URL   : a[href]  → /nos-biens/1-acheter/{id}-{slug}
  - Titre : h3 a
  - Ville : .productCity   (sans code postal en vue liste)
  - Prix  : .productPrice  →  "210 000 €"
  - Photo : img.lazyload[data-src]
  NB : chaque carte apparaît en double (deck "exclu" + deck principal) → dédup par id.

Page détail : blocs étiquetés h4 (label) → h5 (valeur), fiables quel que soit
  le type de bien :
    "Surface" → "80 m²", "Pièces" → "3", "Chambres" → "2",
    "Jardin" → "64 m²" (terrain), "Type" → "Maison",
    "Localisation" → "37000, Tours"  (← CODE POSTAL, indispensable au filtre dept)
  DPE : extrait du texte descriptif ("Classe énergie : C").

Filtre département : POST-FILTRE strict sur code_postal[:2] (le site ne filtre
  pas côté serveur). 0 fuite garantie : un bien sans CP exploitable est rejeté.

Couverture : réseau national à forte présence Centre-Val de Loire
  (Tours / Rochecorbon / Montlouis-sur-Loire, dept 37).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.myfrenchagent.com"
LIST_PATH = "/nos-biens/1-acheter"
TYPE_BIEN = 1            # 1 = maison
MAX_PAGES = 10           # garde-fou (la liste maison fait ~7 pages)
DETAIL_CONCURRENCY = 6   # fetchs détail en parallèle (pour rester dans les temps)
PHOTOS_PER_CARD = 12


# Départements cibles (le post-filtre n'accepte que ceux-là)
TARGET_DEPTS = {"72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"}


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    # On n'accepte JAMAIS hors zone cible (sécurité 0 fuite)
    departements &= TARGET_DEPTS
    if not departements:
        departements = set(TARGET_DEPTS)

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1) collecte des cartes (id, url, ville liste, prix, titre, photo)
        cards = await _collect_cards(client)
        print(f"[MyFrenchAgent] {len(cards)} maisons listées (national)")

        # 2) enrichissement détail (CP + caractéristiques) en parallèle borné
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def _enrich(card):
            async with sem:
                try:
                    return await _fetch_detail(client, card, departements)
                except Exception:
                    return None

        results = await asyncio.gather(*[_enrich(c) for c in cards])

    biens: list[dict] = []
    for bien in results:
        if not bien:
            continue
        # Post-filtre dept STRICT (0 fuite)
        if not bien["code_postal"] or bien["code_postal"][:2] not in departements:
            continue
        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue
        biens.append(bien)

    # Compte par dept (info)
    by_dept: dict[str, int] = {}
    for b in biens:
        by_dept[b["code_postal"][:2]] = by_dept.get(b["code_postal"][:2], 0) + 1
    print(f"[MyFrenchAgent] {len(biens)} retenus après filtre dept : {by_dept}")
    return biens


async def _collect_cards(client: httpx.AsyncClient) -> list[dict]:
    """Pagine la liste nationale des maisons, retourne des cartes dédupliquées."""
    cards: dict[str, dict] = {}
    for page in range(1, MAX_PAGES + 1):
        params = {"idPage": page, "typeBien": TYPE_BIEN}
        r = await client.get(BASE_URL + LIST_PATH, params=params)
        if r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        page_cards = _parse_list_cards(soup)
        if not page_cards:
            break
        new = 0
        for c in page_cards:
            if c["id_annonce"] not in cards:
                cards[c["id_annonce"]] = c
                new += 1
        if new == 0:
            break
        await asyncio.sleep(0.4)
    return list(cards.values())


def _parse_list_cards(soup: BeautifulSoup) -> list[dict]:
    cards: list[dict] = []
    seen: set[str] = set()
    for a in soup.select('a[href*="/nos-biens/1-acheter/"]'):
        href = a.get("href", "")
        m = re.search(r"/nos-biens/1-acheter/(\d+)-", href)
        if not m:
            continue
        aid = m.group(1)
        if aid in seen:
            continue
        card = a.find_parent("div", class_="card")
        if not card:
            continue
        seen.add(aid)

        url = href if href.startswith("http") else BASE_URL + href

        title_el = card.select_one("h3 a") or card.select_one("h3")
        titre = title_el.get_text(" ", strip=True) if title_el else ""

        city_el = card.select_one(".productCity")
        ville = city_el.get_text(" ", strip=True) if city_el else ""

        price_el = card.select_one(".productPrice")
        prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

        photos = []
        img = card.select_one("img.lazyload[data-src]") or card.select_one("img[data-src]")
        if img:
            src = img.get("data-src", "")
            if src and not src.startswith("data:"):
                photos.append(src)

        cards.append(
            {
                "id_annonce": aid,
                "url": url,
                "titre": titre,
                "ville": ville,
                "prix": prix,
                "photos": photos,
            }
        )
    return cards


async def _fetch_detail(
    client: httpx.AsyncClient, card: dict, departements: set[str]
) -> dict | None:
    r = await client.get(card["url"])
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    fields = _labeled_fields(soup)

    # Localisation → "37000, Tours"
    loc = fields.get("localisation", "")
    code_postal, ville_detail = _parse_loc(loc)
    ville = ville_detail or card.get("ville", "")

    # Filtre dept précoce : inutile d'extraire le reste si hors zone
    if not code_postal or code_postal[:2] not in departements:
        return None

    type_bien = (fields.get("type", "") or "maison").strip()
    surface = _parse_m2(fields.get("surface", ""))
    surface_terrain = _parse_m2(fields.get("jardin", ""))
    pieces = _parse_int_simple(fields.get("pieces", ""))
    chambres = _parse_int_simple(fields.get("chambres", ""))
    reference = fields.get("reference de la propriete", "") or card["id_annonce"]

    # Prix : page détail prioritaire, repli sur la liste
    price_el = soup.select_one(".productPrice")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")
    if prix is None:
        prix = card.get("prix")

    # DPE depuis le texte ("Classe énergie : C")
    dpe = _parse_dpe(r.text)

    # Description
    description = _extract_description(soup)

    # Titre : h1 détail prioritaire
    h1 = soup.select_one("h1")
    titre = (h1.get_text(" ", strip=True) if h1 else "") or card.get("titre", "")

    # Photos : galerie détail si plus riche que la liste
    photos = _extract_photos(soup) or card.get("photos", [])

    return {
        "source": "myfrenchagent",
        "url": card["url"],
        "id_annonce": str(reference),
        "titre": titre[:150],
        "type_bien": type_bien.lower(),
        "description": description[:1200],
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": dpe,
        "agence": "My French Agent",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _labeled_fields(soup: BeautifulSoup) -> dict:
    """Récupère les paires h4(label) → h5(valeur) de la fiche détail.

    Labels observés : Référence de la propriété, Surface, Pièces, Chambres,
    Jardin, Type, Localisation. Insensible aux accents/casse pour les clés.
    """
    fields: dict[str, str] = {}
    for h4 in soup.select("h4"):
        label = h4.get_text(" ", strip=True)
        if not label or len(label) > 40:
            continue
        h5 = h4.find_next_sibling("h5") or h4.find_next("h5")
        if not h5:
            continue
        key = _norm_key(label)
        # On ne garde que la première occurrence (bloc principal en haut de page)
        if key not in fields:
            fields[key] = h5.get_text(" ", strip=True)
    return fields


def _norm_key(s: str) -> str:
    s = s.lower()
    repl = (("é", "e"), ("è", "e"), ("ê", "e"), ("à", "a"), ("ç", "c"))
    for a, b in repl:
        s = s.replace(a, b)
    return s.strip()


def _parse_loc(text: str) -> tuple[str, str]:
    """'37000, Tours' → ('37000', 'Tours')"""
    if not text:
        return "", ""
    m = re.search(r"(\d{5})", text)
    cp = m.group(1) if m else ""
    ville = re.sub(r"\d{5}\s*,?\s*", "", text).strip(" ,")
    return cp, ville


def _parse_price(text: str) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d]", "", text.replace("\xa0", ""))
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    # garde-fou : ignore les "prix" aberrants (ex. "6 %" honoraires capté par erreur)
    if v is not None and v < 1000:
        return None
    return v


def _parse_m2(text: str) -> float | None:
    """'80 m²' / '1 174 m² jardin' → 80.0 / 1174.0"""
    if not text:
        return None
    m = re.search(r"([\d\s\xa0]+)\s*m", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
        return f if f > 0 else None
    except ValueError:
        return None


def _parse_int_simple(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


def _parse_dpe(html: str) -> str | None:
    m = re.search(
        r"Classe\s+[ée]nergie\s*:?\s*([A-G])\b", html, re.IGNORECASE
    )
    if m:
        return m.group(1).upper()
    return None


def _extract_description(soup: BeautifulSoup) -> str:
    # og:description ou bloc descriptif
    meta = soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        txt = meta["content"].strip()
        if len(txt) > 40:
            return txt
    for sel in ("[class*=descriptionProd]", ".productDescription", "#description"):
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(" ", strip=True)
            if len(txt) > 40:
                return txt
    return ""


def _extract_photos(soup: BeautifulSoup) -> list[str]:
    photos: list[str] = []
    for img in soup.select("img.lazyload[data-src], img[data-src]"):
        src = img.get("data-src", "")
        if not src or src.startswith("data:"):
            continue
        # ignore les logos d'agent
        if "logoprint" in src or "/agents/" in src:
            continue
        if src not in photos:
            photos.append(src)
    return photos


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
    print(f"\nTotal My French Agent: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['pieces'] or '?'}p — DPE {b['dpe'] or '?'} — {b['ville']}"
        )

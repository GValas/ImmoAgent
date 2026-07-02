"""scrapers/portail_immo.py — Portail Immo (portail/agrégateur d'agences, plateforme Adaptimmo)

Méthode : scrape_simple (httpx) — SSR HTML (page encodée en windows-1252)
URL liste (vente + maison, filtrée par département CÔTÉ SERVEUR) :
    /fr/annonces/vente/maison/{slug}-p-r300-1-1-{NN}-0-{page}.html
    ex : /fr/annonces/vente/maison/sarthe-p-r300-1-1-72-0-1.html
    → la liste ne renvoie que des maisons à vendre du département demandé
      (vérifié : aucune fuite hors-dept ; re-vérifié par post-filtre CP[:2]).

Cartes liste : div.liste-bien-container[data-show-on-map=<id>]
  - id_annonce : attribut data-show-on-map (id numérique adaptimmo)
  - URL détail : a#lienphoto[href]  → /fr/annonce/vente-maison-{ville}-p-r7-{id}.html
  - Type      : h2.liste-bien-type  (Maison, Maison de village, Moulin, Pavillon…)
  - Ville     : h3.liste-bien-ville  (en MAJUSCULES)
  - Prix      : <costpermonth data-price="345 450"> ou div.liste-bien-price "Prix : 345 450 €"
  - Photos    : img.liste-bien-photo-slideshow[data-src] (assets.adaptimmo.com)

La liste NE porte PAS le code postal ni la surface. On enrichit chaque bien
survivant (après pré-filtre prix) par sa page détail :
  - <title> "Vente maison Vibraye, 160m² 10 pièces 345 450€ …" → surface, pièces
  - <meta keywords> "…Sarthe,(72320)…" → code postal  → post-filtre strict CP[:2]==dept
  - terrain / DPE : extraits du texte de la page détail quand présents.

Particularités :
  - Encodage windows-1252 (cp1252) — on force r.encoding avant .text.
  - 12 cartes / page ; pagination via dernier segment numérique de l'URL.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.portail-immo.fr"
MAX_PAGES = 12
PHOTOS_PER_CARD = 12
DETAIL_CONCURRENCY = 6


# Code département → slug d'URL portail-immo.fr
DEPT_SLUGS: dict[str, str] = {
    "72": "sarthe",
    "28": "eure-et-loir",
    "45": "loiret",
    "89": "yonne",
    "49": "maine-et-loire",
    "37": "indre-et-loire",
    "36": "indre",
    "18": "cher",
    "58": "nievre",
    "41": "loir-et-cher",
    "53": "mayenne",
}

# Types explicitement exclus (la liste est déjà "maison" mais par prudence)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|boutique|tabac|bar|restaurant|boulangerie",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, slug, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[PortailImmo] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[PortailImmo] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    slug: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    # 1) Collecte des cartes liste (cheap fields) avec pré-filtre prix
    cartes: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/fr/annonces/vente/maison/{slug}-p-r300-1-1-{dept}-0-{page}.html"
        r = await client.get(url)
        if r.status_code != 200:
            break
        r.encoding = "windows-1252"

        cards = BeautifulSoup(r.text, "html.parser").select(
            "div.liste-bien-container"
        )
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            bien = _parse_card(card, dept)
            if not bien:
                continue
            aid = bien["id_annonce"]
            if aid in seen_ids:
                continue

            # Pré-filtre prix (le prix est fiable dès la liste)
            p = bien.get("prix") or 0
            if prix_max and p and p > prix_max:
                seen_ids.add(aid)
                continue
            if prix_min and p and p < prix_min:
                seen_ids.add(aid)
                continue

            seen_ids.add(aid)
            cartes.append(bien)
            new_on_page += 1

        if new_on_page == 0:
            break
        await asyncio.sleep(0.5)

    # 2) Enrichissement détail (CP / surface / pièces / terrain / DPE) + post-filtre strict
    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def _enrich(bien: dict):
        async with sem:
            await _fill_from_detail(client, bien)

    await asyncio.gather(*(_enrich(b) for b in cartes))

    # 3) Filtres durs : département strict + surface_min
    biens: list[dict] = []
    for bien in cartes:
        cp = bien.get("code_postal") or ""
        # 0 fuite : on n'accepte que le département cible quand le CP est connu.
        if cp and cp[:2] != dept:
            continue
        s = bien.get("surface") or 0
        if surface_min and s and s < surface_min:
            continue
        biens.append(bien)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    aid = card.get("data-show-on-map", "").strip()
    if not aid:
        return None

    link = card.select_one("a#lienphoto") or card.select_one(
        "a[href*='/fr/annonce/']"
    )
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien
    type_el = card.select_one("h2.liste-bien-type")
    type_bien = type_el.get_text(" ", strip=True) if type_el else "maison"
    if _EXCLUDE_TYPE.search(type_bien):
        return None

    # Ville (MAJUSCULES dans la liste) → on remet en casse titre
    ville_el = card.select_one("h3.liste-bien-ville")
    ville_raw = ville_el.get_text(" ", strip=True) if ville_el else ""
    ville = ville_raw.title() if ville_raw else ""

    # Prix : <costpermonth data-price="345 450"> ou texte "Prix : 345 450 €"
    prix = None
    cpm = card.select_one("costpermonth[data-price]")
    if cpm:
        prix = _parse_price(cpm.get("data-price", ""))
    if prix is None:
        price_el = card.select_one("div.liste-bien-price")
        if price_el:
            prix = _parse_price(price_el.get_text(" ", strip=True))

    # Titre
    titre = f"{type_bien} {ville}".strip()

    # Photos
    photos = []
    for img in card.select("img.liste-bien-photo-slideshow"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:") and "anti-cheat" not in src:
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "portail_immo",
        "url": url,
        "id_annonce": aid,
        "titre": titre[:150],
        "type_bien": type_bien.lower(),
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",          # rempli par la page détail
        "surface": None,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Portail Immo",
    }


async def _fill_from_detail(client: httpx.AsyncClient, bien: dict) -> None:
    """Récupère CP / surface / pièces / terrain / DPE / description sur la page détail."""
    try:
        r = await client.get(bien["url"])
        if r.status_code != 200:
            return
        r.encoding = "windows-1252"
        html = r.text
    except Exception:
        return

    # Code postal : meta keywords "…Sarthe,(72320)…" (ou tout (NNNNN) du titre/meta)
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", html)
    if m_cp:
        cp = m_cp.group(1)
    if cp:
        bien["code_postal"] = cp

    soup = BeautifulSoup(html, "html.parser")

    # <title> "Vente maison Vibraye, 160m² 10 pièces 345 450€ avec terrasse"
    title_txt = soup.title.get_text(" ", strip=True) if soup.title else ""
    if bien.get("surface") is None:
        m = re.search(r"(\d[\d\s\xa0]*)\s*m(?:²|&sup2;|2)", title_txt)
        if m:
            bien["surface"] = _to_float(m.group(1))
    if bien.get("pieces") is None:
        m = re.search(r"(\d+)\s*pi[èe]ce", title_txt, re.IGNORECASE)
        if m:
            bien["pieces"] = int(m.group(1))

    # Surface en secours : bloc bien-specs ("Surface … 160 m²")
    if bien.get("surface") is None:
        m = re.search(r"Surface\s*</span>.*?([\d\s\xa0]+)\s*m(?:²|&sup2;)", html, re.S)
        if m:
            bien["surface"] = _to_float(m.group(1))

    # Chambres
    m = re.search(r"(\d+)\s*chambre", html, re.IGNORECASE)
    if m:
        bien["chambres"] = int(m.group(1))

    # Terrain : "terrain de 2 500 m²" / "Terrain … 2500 m²"
    m = re.search(r"[Tt]errain[^0-9]{0,20}([\d][\d\s\xa0]{1,})\s*m(?:²|&sup2;|2)", html)
    if m:
        t = _to_float(m.group(1))
        if t and t >= 50:
            bien["surface_terrain"] = t

    # DPE : lettre A-G associée à conso/énergie/DPE
    m = re.search(
        r"(?:classe[\s\-]?(?:énergie|energie)|consommation|DPE)[^A-G]{0,40}\b([A-G])\b",
        html,
        re.IGNORECASE,
    )
    if m:
        bien["dpe"] = m.group(1).upper()

    # Description : meta description
    meta_desc = soup.find("meta", attrs={"name": "Description"}) or soup.find(
        "meta", attrs={"name": "description"}
    )
    if meta_desc and meta_desc.get("content"):
        bien["description"] = meta_desc["content"].strip()[:1200]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    # Garde-fou : un "prix" < 1000 est probablement un €/mois ou un bruit
    if v is not None and v < 1000:
        return None
    return v


def _to_float(text: str) -> float | None:
    cleaned = re.sub(r"[\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
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
    print(f"\nTotal Portail Immo: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    sans_cp = sum(1 for b in biens if not b["code_postal"])
    if sans_cp:
        print(f"(dont {sans_cp} sans code postal récupéré)")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal'] or '?????'}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — DPE {b.get('dpe') or '?'}"
            f" — {b['type_bien']} — {b['ville']}"
        )

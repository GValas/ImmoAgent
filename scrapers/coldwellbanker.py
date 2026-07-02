"""scrapers/coldwellbanker.py — Coldwell Banker France (réseau de prestige)

Méthode : scrape_simple (httpx) — SSR HTML.
Cloudflare en frontal mais CDN only (pas de challenge) : httpx + UA navigateur → 200.

URL pattern : /vente+immobilier+{dept-slug}+dep{NN}.html      (page 1)
              /vente+immobilier+{dept-slug}+dep{NN}-p{N}.html  (pages suivantes)
              → filtre département CÔTÉ SERVEUR (vérifié : aucune fuite hors-dept ;
                chaque fiche porte le token +{NN}+ et ?dep={NN} dans son URL).

Cards : article.annonce-container (12/page)
  - URL fiche : a[href*="+vente+r{ID}.html"]  → /{ville}+{NN}+{type}+vente+r{ID}.html?...&dep={NN}
  - id annonce : data-ac-offre_id sur l'article
  - type+ville : span.title-line-2  ("Maison Montbizot")
  - prix    : li.prix span.item-title  ("2 599 800 €")
  - surface : li.surface_habitable  ("441,6 m² de surface")
  - chambres: li.* item-title "chambres" (texte de la card : "5 chambres")
  - desc    : p.annonce-desc-texte
  - photos  : img (data-src / src)

Le code postal complet n'apparaît pas sur la liste : seul le département (2 chiffres)
est garanti (token URL). `code_postal` est laissé à "" ou au dept 2 chiffres, et
`departement` est fiable. On ne garde que maisons / propriétés / châteaux / manoirs.

Couverture : prestige, implantation inégale. Sur les depts cibles (2026-05) :
  72≈41, 37≈11, 49≈4, 53≈3, 36≈1 ; 28/45/89/18/58/41 = 0.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int

BASE_URL = "https://www.coldwellbanker.fr"
MAX_PAGES = 12
PHOTOS_PER_CARD = 8


# Code département → slug URL (segment "ville/region" de l'URL Coldwell Banker)
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

# Types à conserver (maisons / propriétés / demeures) ; appartements & terrains exclus.
_KEEP_TYPE = re.compile(
    r"maison|villa|pavillon|propri[ée]t[ée]|domaine|ch[âa]teau|manoir|long[èe]re|"
    r"ferme|demeure|moulin|gentilhommi[èe]re|mas|gite|g[îi]te|corps de ferme|chartreuse",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|loft|terrain|immeuble|local|commerce|garage|parking|"
    r"bureau|fonds|b[âa]timent",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

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
                print(f"[ColdwellBanker] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[ColdwellBanker] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.5)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    slug: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = f"{BASE_URL}/vente+immobilier+{slug}+dep{dept}.html"
        else:
            url = f"{BASE_URL}/vente+immobilier+{slug}+dep{dept}-p{page}.html"

        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select("article.annonce-container")
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            try:
                bien = _parse_card(card, dept)
            except Exception:
                continue
            if not bien:
                continue

            # Sécurité anti-fuite : le token dept de l'URL fiche DOIT matcher.
            if bien["departement"] != dept:
                continue

            aid = bien["id_annonce"]
            if aid in seen_ids:
                continue

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            seen_ids.add(aid)
            biens.append(bien)
            new_on_page += 1

        if new_on_page == 0:
            break

        await asyncio.sleep(0.4)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one('a[href*="+vente+r"]') or card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href or "+vente+r" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Département depuis le token URL : /{ville}+{NN}+{type}+vente+r{ID}.html?...&dep={NN}
    dept_url = ""
    m_d = re.search(r"\+(\d{2})\+[^+]*\+vente\+r\d+", url)
    if m_d:
        dept_url = m_d.group(1)
    else:
        m_dq = re.search(r"[?&]dep=(\d{2})", url)
        dept_url = m_dq.group(1) if m_dq else ""

    # Type de bien depuis le slug d'URL : segment entre le dept et "+vente"
    type_url = ""
    m_t = re.search(r"\+\d{2}\+([a-z0-9-]+)\+vente\+r\d+", url)
    if m_t:
        type_url = m_t.group(1).replace("-", " ")

    # id annonce
    aid = card.get("data-ac-offre_id") or ""
    if not aid:
        m_id = re.search(r"\+vente\+r(\d+)\.html", url)
        aid = m_id.group(1) if m_id else url

    # Titre : "Maison Montbizot" (type + ville)
    t2 = card.select_one(".title-line-2")
    title_loc = t2.get_text(" ", strip=True) if t2 else ""

    # ville : retire le 1er mot (le type) du title-line-2
    ville = ""
    if title_loc:
        parts = title_loc.split()
        if len(parts) > 1:
            ville = " ".join(parts[1:]).strip()
        else:
            ville = title_loc.strip()

    type_bien = type_url or (title_loc.split()[0].lower() if title_loc else "maison")

    # Filtre type : on ne garde que maisons / propriétés
    type_test = f"{type_url} {title_loc}"
    if _EXCLUDE_TYPE.search(type_test) and not _KEEP_TYPE.search(type_test):
        return None
    if not _KEEP_TYPE.search(type_test):
        return None

    # Prix
    prix = None
    price_el = card.select_one("li.prix .item-title") or card.select_one("li.prix")
    if price_el:
        prix = _parse_num(price_el.get_text(" ", strip=True))

    # Surface habitable
    surface = None
    surf_el = card.select_one("li.surface_habitable")
    if surf_el:
        surface = _parse_num(surf_el.get_text(" ", strip=True))

    # Chambres / pièces depuis le texte de la card ("5 chambres 5 sde")
    card_text = card.get_text(" ", strip=True)
    chambres = _parse_int(r"(\d+)\s*chambres?", card_text)
    pieces = _parse_int(r"(\d+)\s*pi[eè]ces?", card_text)

    # Description
    desc_el = card.select_one("p.annonce-desc-texte")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Titre lisible
    titre = title_loc or f"{type_bien.title()} {ville}".strip()

    # Photos
    photos = []
    for img in card.find_all("img"):
        src = (
            img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("data-original")
            or img.get("src")
            or ""
        )
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http"):
                photos.append(src)
    # dédup en gardant l'ordre
    photos = list(dict.fromkeys(photos))[:PHOTOS_PER_CARD]

    return {
        "source": "coldwellbanker",
        "url": url,
        "id_annonce": str(aid),
        "titre": titre[:150],
        "type_bien": type_bien[:40],
        "description": description[:1200],
        "departement": dept_url or dept,
        "ville": ville[:80],
        "code_postal": "",  # CP complet absent de la liste ; dept garanti
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "dpe": None,
        "photos": photos,
        "agence": "Coldwell Banker",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_num(text: str) -> float | None:
    """'2 599 800 €' / '441,6 m² de surface' → float"""
    if not text:
        return None
    t = text.replace("\xa0", " ").replace(" ", " ")
    # garde chiffres, espaces, virgule décimale ; coupe à la 1re unité
    m = re.search(r"([\d\s]+(?:,\d+)?)", t)
    if not m:
        return None
    cleaned = m.group(1).replace(" ", "").replace(",", ".")
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
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
    print(f"\nTotal Coldwell Banker: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    # contrôle de fuite
    cibles = {str(d).zfill(2) for d in criteres.departements}
    fuites = [b for b in biens if b["departement"] not in cibles]
    print(f"FUITES hors-dept : {len(fuites)}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('chambres') or '?'}ch"
            f" — {b['type_bien']} — {b['ville']}"
        )

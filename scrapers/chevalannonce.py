"""scrapers/chevalannonce.py — ChevalAnnonce (portail équestre, rubrique immobilier)

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /annonces/fr/{dept-slug}/immobilier-equestre/[?p=N]
              (ex : /annonces/fr/yonne/immobilier-equestre/)
              → filtre département CÔTÉ SERVEUR via le slug de département
                (vérifié : chaque page de dept ne renvoie QUE ce département,
                 aucune fuite hors-dept).

Particularités :
  - Portail communautaire équestre (haras, écuries, fermes équestres,
    propriétés équestres). Rubrique « immobilier-equestre » = ventes + locations
    mélangées → on ne garde QUE « Type : Vente ».
  - Inventaire faible mais réel sur la zone Val-de-Loire / Ouest (niche).

Cartes : li.searchResult
  - Lien/titre : a.resultLnk[href]  → /{id}-{slug}
  - Bloc info  : .bloc2info  →  texte segmenté :
        "<titre>" | "(NN) <Dept> - France" | "Nature du bien : <X>" | "Type : Vente|Location"
  - Code dept  : parenthèses "(NN)" dans .bloc2info (post-filtre strict CP[:2]/dept)
  - Nature     : "Nature du bien : Propriétés équestres / Fermes équestres / Terrains / Maison…"
                 → type_bien ; les « Terrains » sont exclus.
  - Code postal: parfois présent dans le titre ("(89150)") → extrait si dispo, sinon None.
  - Prix       : .bloc3price .price  →  "318000 €"
  - Photos     : img de .bloc1photo (1 vignette en liste).

Surface / pièces / terrain : non exposés en liste sur ce portail → None
(enrichis plus tard en page détail par gallery.py si besoin).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.chevalannonce.com"
MAX_PAGES = 5


# Code département → slug URL chevalannonce.com/annonces/fr/{slug}/immobilier-equestre/
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

# Natures de bien (segment "Nature du bien : …") à exclure (pas du bâti habitable)
_EXCLUDE_NATURE = re.compile(r"terrain|pension|herbage|p[aâ]ture", re.IGNORECASE)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)  # non exposé en liste, gardé pour cohérence

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                continue
            try:
                biens = await _scrape_dept(client, dept, slug, prix_max, prix_min)
                results.extend(biens)
                print(f"[ChevalAnnonce] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[ChevalAnnonce] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.5)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    slug: str,
    prix_max: int,
    prix_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/annonces/fr/{slug}/immobilier-equestre/"
        if page > 1:
            url += f"?p={page}"
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select("li.searchResult")
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

            # Post-filtre dept STRICT : le code (NN) du bloc info doit == dept cible.
            if bien["departement"] != dept:
                continue
            if bien["code_postal"] and bien["code_postal"][:2] != dept:
                continue

            aid = bien["id_annonce"]
            if aid in seen_ids:
                continue

            p = bien.get("prix") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue

            seen_ids.add(aid)
            biens.append(bien)
            new_on_page += 1

        if new_on_page == 0:
            break
        await asyncio.sleep(0.4)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a.resultLnk") or card.select_one(".bloc2info a[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce : préfixe numérique du slug (/{id}-...)
    m_id = re.match(r"/?(\d+)-", href)
    id_annonce = m_id.group(1) if m_id else url

    info_el = card.select_one(".bloc2info")
    if not info_el:
        return None
    # Segments séparés par '|' : titre | "(NN) Dept - France" | "Nature du bien : X" | "Type : Y"
    segs = [s.strip() for s in info_el.get_text("|", strip=True).split("|") if s.strip()]
    info_text = " ".join(segs)

    titre = segs[0] if segs else ""

    # Type : on ne garde que les VENTES
    type_vente = "vente" in info_text.lower() and "type : vente" in info_text.lower()
    if not type_vente:
        # repli : si "Type :" présent et != Vente → écarte ; sinon (Type absent) on garde
        if re.search(r"type\s*:\s*location", info_text, re.IGNORECASE):
            return None

    # Code département depuis "(NN)"
    m_dep = re.search(r"\((\d{2,3})\)\s*[A-Za-zÀ-ÿ]", info_text)
    dep_code = ""
    if m_dep:
        dep_code = m_dep.group(1).zfill(2)[:2]
    # secours : tout "(NN)" présent
    if not dep_code:
        m_any = re.search(r"\((\d{2})\)", info_text)
        if m_any:
            dep_code = m_any.group(1)
    departement = dep_code or dept

    # Nature du bien → type_bien (cherché segment par segment pour ne pas
    # déborder sur le segment "Type : …")
    nature = ""
    for seg in segs:
        m_nat = re.match(r"Nature du bien\s*:\s*(.+)", seg, re.IGNORECASE)
        if m_nat:
            nature = m_nat.group(1).strip()
            break
    if nature and _EXCLUDE_NATURE.search(nature):
        return None
    type_bien = (nature or "propriete equestre").lower()

    # Code postal éventuellement dans le titre : "(89150)"
    code_postal = ""
    m_cp = re.search(r"\((\d{5})\)", titre)
    if m_cp:
        code_postal = m_cp.group(1)

    # Ville : best-effort — souvent absente proprement en liste → None
    ville = ""

    # Prix
    price_el = card.select_one(".bloc3price .price") or card.select_one(".price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Photo vignette
    photos = []
    img = card.select_one(".bloc1photo img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith(".."):
                src = BASE_URL + "/" + src.lstrip("./")
            elif src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)

    return {
        "source": "chevalannonce",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien[:60],
        "description": "",
        "departement": departement,
        "ville": ville[:80] or "",
        "code_postal": code_postal,
        "surface": None,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "ChevalAnnonce",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

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
    print(f"\nTotal ChevalAnnonce: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    cps = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Préfixes CP vus  : {cps}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b['type_bien']}"
            f" — CP {b['code_postal'] or '?'}"
        )

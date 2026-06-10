"""scrapers/barnes_proprietes_chateaux.py — Barnes Propriétés & Châteaux

Méthode : scrape_simple (httpx) — SSR HTML (WordPress + Elementor, listing apimo).
Site de prestige rural (maisons de caractère, domaines, châteaux), couverture NATIONALE.

URL pattern : /vente/maisons-de-caractere/page/{N}   (≈48 pages, ≈12 cartes utiles/page)
  → On scrape le LISTING NATIONAL puis POST-FILTRE strict sur le département.
  ⚠️ NE PAS utiliser le slug région d'URL : le filtre régional est lâche
     (une page « région X » vide retombe en fallback national). Le seul filtre
     fiable est le département de CHAQUE carte (post-filtre ci-dessous).

Cartes : article.bien (Elementor loop). Une carte vide (sans h2) = placeholder grille → ignorée.
  - URL    : h2 a[href]  → /annonce/{id}
  - Titre  : h2 a
  - Infos  : .infos-bien-card  →  "Type - [N CHAMBRES] - SURFACE M² - [TERRAIN M²|N,N HA] LOCALISATION : Ville"
  - Prix   : .prix-bien-card   →  "750 000 €"
  - Dept   : .voir-annonces-card a[href]  →  ".../...-cote-d-or+21"  (code dept après le '+')
             Source de filtre département la plus fiable (code numérique explicite).
             Repli : nom de département mappé (DEPT_NOMS) si le code '+NN' manque.
  - Photo  : img.img-preview-bien[src]

Pas de code postal dans la liste (seulement la ville + le département) → on remplit
`code_postal=None` et on renseigne `departement` = code à 2 chiffres.

Filtre département STRICT : on ne garde que les cartes dont le code dept ∈ départements cibles
→ objectif 0 fuite hors-zone (vérifié sur le listing national).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import unicodedata

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.barnes-proprietes-chateaux.com"
LISTING_PATH = "/vente/maisons-de-caractere/page/{page}"
MAX_PAGES = 48
PHOTOS_PER_CARD = 1  # la liste n'expose qu'une image de preview (galerie en page détail)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Repli : nom de département (sans accent, minuscule) → code, pour les départements cibles
DEPT_NOMS: dict[str, str] = {
    "sarthe": "72",
    "eure-et-loir": "28",
    "loiret": "45",
    "yonne": "89",
    "maine-et-loire": "49",
    "indre-et-loire": "37",
    "indre": "36",
    "cher": "18",
    "nievre": "58",
    "loir-et-cher": "41",
    "mayenne": "53",
}


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()
    per_dept: dict[str, int] = {}

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = BASE_URL + LISTING_PATH.format(page=page)
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Barnes] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("article.bien")
            if not cards:
                break

            real_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue
                real_on_page += 1

                dept = bien["departement"]
                # Post-filtre département STRICT (0 fuite hors-zone)
                if dept not in departements:
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
                results.append(bien)
                per_dept[dept] = per_dept.get(dept, 0) + 1

            # Plus aucune vraie carte sur la page → fin de la pagination
            if real_on_page == 0:
                break

            await asyncio.sleep(0.35)

    summary = ", ".join(f"{d}:{n}" for d, n in sorted(per_dept.items())) or "aucun"
    print(f"[Barnes] Total {len(results)} annonces (par dept cible: {summary})")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("h2 a")
    if not link:
        # carte vide (placeholder grille Elementor) → on ignore
        return None
    href = link.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce depuis /annonce/{id}
    m_id = re.search(r"/annonce/(\d+)", url)
    id_annonce = m_id.group(1) if m_id else url

    titre = link.get_text(" ", strip=True)

    # Département : code numérique dans le href du lien "Voir toutes nos annonces ... +NN"
    dept = _extract_dept(card)
    if not dept:
        return None

    # Infos : "Type - [N CHAMBRES] - SURFACE M² - [TERRAIN] LOCALISATION : Ville"
    info_el = card.select_one(".infos-bien-card .elementor-widget-container")
    info = info_el.get_text(" ", strip=True) if info_el else ""
    type_bien, chambres, surface, surface_terrain, ville = _parse_infos(info)
    if not type_bien:
        type_bien = "maison de caractère"

    # Prix
    prix_el = card.select_one(".prix-bien-card .elementor-widget-container")
    prix = _parse_price(prix_el.get_text(" ", strip=True) if prix_el else "")

    # Photo de preview
    photos: list[str] = []
    img = card.select_one("img.img-preview-bien")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "barnes_proprietes_chateaux",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": info[:1200],
        "departement": dept,
        "ville": (ville or "")[:80],
        "code_postal": None,  # non exposé dans la liste (ville + dept seulement)
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,       # non exposé (seulement nb de chambres)
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Barnes Propriétés & Châteaux",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def _extract_dept(card) -> str | None:
    """Code département à 2 chiffres depuis le lien '.voir-annonces-card a'.

    href type : /vente/maisons-de-campagne-cote-d-or+21  → '21'
    Repli : nom de département dans le slug (DEPT_NOMS).
    """
    a = card.select_one(".voir-annonces-card a")
    href = a.get("href", "") if a else ""
    if not href:
        return None
    # Code numérique explicite après le '+'
    m = re.search(r"\+(\d{2,3})$", href.strip())
    if m:
        code = m.group(1)
        # Corse 2A/2B exclus de notre zone ; on garde les 2 premiers chiffres
        return code[:2] if len(code) <= 3 else code[:2]
    # Repli : reconnaître un nom de département cible dans le slug
    slug = _strip_accents(href.lower())
    for nom, code in DEPT_NOMS.items():
        if nom in slug:
            return code
    return None


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_infos(info: str):
    """'Maison de campagne - 4 CHAMBRES - 260 M² - 1840 M² LOCALISATION : Ville'
    → (type_bien, chambres, surface_hab, surface_terrain, ville)

    - Type = texte avant le 1er ' - '
    - chambres = 'N CHAMBRES'
    - surface habitable = 1ʳᵉ valeur 'N M²'
    - terrain = 'N,N HA' (×10000) ou 2ᵉ valeur 'N M²'
    - ville = après 'LOCALISATION :'
    """
    type_bien = chambres = surface = surface_terrain = ville = None
    if not info:
        return type_bien, chambres, surface, surface_terrain, ville

    # Ville
    m_loc = re.search(r"LOCALISATION\s*:\s*(.+)$", info, re.IGNORECASE)
    if m_loc:
        ville = m_loc.group(1).strip()
    head = re.split(r"LOCALISATION", info, flags=re.IGNORECASE)[0]

    # Type : avant le 1er séparateur ' - '
    first = re.split(r"\s-\s", head, maxsplit=1)[0].strip()
    if first:
        type_bien = first

    # Chambres
    m_ch = re.search(r"(\d+)\s*CHAMBRES?", head, re.IGNORECASE)
    if m_ch:
        chambres = int(m_ch.group(1))

    # Surface habitable : première occurrence 'N M²'
    surfaces_m2 = [
        float(re.sub(r"[\s\xa0]", "", v))
        for v in re.findall(r"([\d\s\xa0]+)\s*M²", head, re.IGNORECASE)
    ]
    if surfaces_m2:
        surface = surfaces_m2[0]

    # Terrain : 'N,N HA' prioritaire, sinon 2ᵉ valeur en M²
    m_ha = re.search(r"([\d]+(?:[.,]\d+)?)\s*HA", head, re.IGNORECASE)
    if m_ha:
        try:
            surface_terrain = float(m_ha.group(1).replace(",", ".")) * 10000
        except ValueError:
            pass
    elif len(surfaces_m2) >= 2:
        surface_terrain = surfaces_m2[1]

    return type_bien, chambres, surface, surface_terrain, ville


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
    print(f"\nTotal Barnes Propriétés & Châteaux: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

"""scrapers/stonimmo.py — Stonimmo (portail/agrégateur réseau mandataires)

Méthode : scrape_simple (httpx) — SSR HTML (Tailwind/Next.js rendu serveur).
URL pattern : /immobilier/achat/{nom-dept}-{NN}-d/maison/?page={N}
              ex: /immobilier/achat/sarthe-72-d/maison/
              → filtre département + catégorie « maison » CÔTÉ SERVEUR.
                 (vérifié : aucune fuite hors-dept — sur 72 et 45, 100 % des CP
                  commencent par le bon code.)

Cartes : a[href*="/immobilier/annonces/"]  (l'ancre EST la carte)
  - URL   : href  → /immobilier/annonces/{slug}-idx{TOKEN}/
  - Titre : h3  →  "Maison à vendre - 6 pièces - 106m2 - 217 300,00 €"
            (type, pièces, surface, prix sont encodés dans ce titre)
  - Loc   : p > span  →  "Ville  CODEPOSTAL" (ex: "Saint-Saturnin 72650")
  - Texte : p.text-gray-500 (description tronquée)
  - Photo : img[src] (cloudfront)
  - id    : token "idx..." du slug d'URL

Type de bien : la catégorie /maison/ regroupe maisons + propriétés. On
               post-filtre quand même pour exclure tout appartement/terrain
               qui passerait.

Pagination : ?page={N}. ~30 cartes/page. On s'arrête quand une page n'apporte
             plus de nouvel id (overlap nul observé entre pages successives).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.stonimmo.com"
MAX_PAGES = 8
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Code département → slug URL stonimmo (/immobilier/achat/{slug}-{NN}-d/maison/)
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

# Types à exclure même s'ils remontent dans la catégorie maison
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
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
                print(f"[Stonimmo] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Stonimmo] Erreur dept {dept}: {e}")
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
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/immobilier/achat/{slug}-{dept}-d/maison/?page={page}"
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select(
            'a[href*="/immobilier/annonces/"]'
        )
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

            # Filtre département STRICT (filtre serveur déjà OK, on re-vérifie)
            if bien["code_postal"] and bien["code_postal"][:2] != dept:
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

        # Plus aucun id inédit → fin de pagination
        if new_on_page == 0:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    href = card.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce : token "idx..." du slug
    m_id = re.search(r"idx([A-Za-z0-9_-]+)", href)
    id_annonce = m_id.group(1) if m_id else url

    # Titre : "Maison à vendre - 6 pièces - 106m2 - 217 300,00 €"
    title_el = card.select_one("h3")
    titre_brut = title_el.get_text(" ", strip=True) if title_el else ""

    # Type de bien (1er segment du titre, avant "à vendre")
    type_bien = "maison"
    m_type = re.match(r"^([A-Za-zÀ-ÿ' -]+?)\s+à vendre", titre_brut)
    if m_type:
        type_bien = m_type.group(1).strip().lower()
    if _EXCLUDE_TYPE.search(type_bien) or _EXCLUDE_TYPE.search(titre_brut):
        return None

    # Localisation : "Ville  CODEPOSTAL"
    loc_el = card.select_one("p span")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)

    # Description
    desc_el = card.select_one("p.text-gray-500")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Pièces / surface / prix depuis le titre
    pieces = _parse_int(r"(\d+)\s*pi[eè]ces?", titre_brut)
    surface = _parse_surface(titre_brut)
    prix = _parse_price(titre_brut)

    # Titre lisible (sans le prix collé) ; à défaut, fabriqué
    titre = re.sub(r"\s*-\s*[\d\s\xa0]+,\d{2}\s*€\s*$", "", titre_brut).strip()
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Photos
    photos = []
    for img in card.select("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "stonimmo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Stonimmo",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Saint-Saturnin 72650' → ('Saint-Saturnin', '72650')"""
    cp = ""
    m_cp = re.search(r"\b(\d{5})\b", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\b\d{5}\b\s*", " ", text).strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    """'... - 217 300,00 €' → 217300.0"""
    m = re.search(r"([\d\s\xa0]+(?:,\d{2})?)\s*€", text)
    if not m:
        return None
    raw = m.group(1)
    cleaned = re.sub(r"[\s\xa0]", "", raw).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'... - 106m2 ...' ou '106 m²' → 106.0"""
    m = re.search(r"(\d[\d\s\xa0]*)\s*m[²2]", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 5 <= f <= 5000:
                return f
        except ValueError:
            pass
    return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


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
    print(f"\nTotal Stonimmo: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

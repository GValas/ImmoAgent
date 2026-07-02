"""scrapers/mapropriete.py — Ma-Propriété.fr (immobilier rural de prestige / équestre)

Méthode : scrape_simple (httpx) — SSR HTML (Symfony).
Spécialiste : châteaux, manoirs, maisons de maître, demeures, propriétés
équestres / forestières / viticoles. Inventaire de niche (quelques biens / dept).

URL pattern (PAS de /maison/{dept} → 404) :
    /fr/{categorie}/departement/{dept-slug}
  catégories d'entrée confirmées : `prestige` et `equestre`.
  Le listing d'un dept mélange en réalité toutes les catégories du dept
  (prestige, equestre, forestiere, touristique, viticole…), donc on interroge
  les 2 points d'entrée prestige+equestre puis on déduplique par URL.

Filtre département : le slug du département est le 4e segment de la fiche
    /fr/{categorie}/{sous-categorie}/{dept-slug}/{advert-slug}
  → on filtre en DUR sur `parts[3] == dept-slug`.
  ATTENTION : quand un dept est vide, la page affiche le message
  « Aucune annonce ne correspond… » PUIS des cartes de SUGGESTION d'AUTRES
  départements (fuite). Le filtre par slug d'URL ci-dessus écarte ces
  suggestions de façon fiable (vérifié : 0 fuite hors-dept).

Cartes : div.c-cardAnnonce
  - URL/titre : a.c-cardAnnonce__link[href][title]
  - dept      : .c-cardAnnonce__location__area  (nom du département)
  - titre     : .c-cardAnnonce__location__name
  - prix      : .c-cardAnnonce__info__price   ("1 298 000 €")
  - terrain   : .c-cardAnnonce__location__size ("0.4 ha" → m²)
  - texte     : .c-cardAnnonce__info__text
  - photo     : .c-cardAnnonce__image img[src]

Surface habitable / pièces / code postal : pas de champ dédié → extraits du
titre + texte (regex « 418 m² habitables », « 14 pièces », « (49400) »…).
Pas de pagination listing dept (≤ ~10 cartes/cat, inventaire faible).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.ma-propriete.fr"
CATEGORIES = ("prestige", "equestre")   # points d'entrée confirmés
PHOTOS_PER_CARD = 1


# Code département → slug URL ma-propriete.fr
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

_TYPE_MAP = [
    (re.compile(r"château|chateau", re.IGNORECASE), "château"),
    (re.compile(r"manoir", re.IGNORECASE), "manoir"),
    (re.compile(r"maison de ma[îi]tre|demeure", re.IGNORECASE), "maison de maître"),
    (re.compile(r"longère|longere", re.IGNORECASE), "longère"),
    (re.compile(r"propriété|propriete|domaine", re.IGNORECASE), "propriété"),
    (re.compile(r"moulin", re.IGNORECASE), "moulin"),
    (re.compile(r"villa", re.IGNORECASE), "villa"),
    (re.compile(r"maison", re.IGNORECASE), "maison"),
]


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    results: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                continue
            n_dept = 0
            for cat in CATEGORIES:
                url = f"{BASE_URL}/fr/{cat}/departement/{slug}"
                try:
                    r = await client.get(url)
                    if r.status_code != 200:
                        continue
                except Exception as e:
                    print(f"[MaPropriete] Erreur {cat}/{dept}: {e}")
                    continue

                soup = BeautifulSoup(r.text, "html.parser")
                for card in soup.select(".c-cardAnnonce"):
                    bien = _parse_card(card, dept, slug)
                    if not bien:
                        continue

                    # filtre prix / surface
                    p = bien.get("prix") or 0
                    s = bien.get("surface") or 0
                    if prix_max and p and p > prix_max:
                        continue
                    if prix_min and p and p < prix_min:
                        continue
                    if surface_min and s and s < surface_min:
                        continue

                    aid = bien["url"]
                    if aid in seen:
                        continue
                    seen.add(aid)
                    results.append(bien)
                    n_dept += 1

                await asyncio.sleep(0.4)

            print(f"[MaPropriete] Dept {dept}: {n_dept} annonces")

    return results


def _parse_card(card, dept: str, dept_slug: str) -> dict | None:
    link = card.select_one("a.c-cardAnnonce__link")
    if not link or not link.get("href"):
        return None
    href = link["href"].strip()

    # FILTRE DEPARTEMENT EN DUR : le slug dept est le 4e segment de la fiche
    # /fr/{categorie}/{sous-categorie}/{dept-slug}/{advert-slug}
    parts = [p for p in href.split("/") if p]
    href_dept_slug = parts[3] if len(parts) > 3 else ""
    if href_dept_slug != dept_slug:
        # carte de suggestion d'un autre département → on jette
        return None

    url = href if href.startswith("http") else BASE_URL + href

    titre = (link.get("title") or "").strip()
    name_el = card.select_one(".c-cardAnnonce__location__name")
    if not titre and name_el:
        titre = name_el.get_text(" ", strip=True)
    titre = re.sub(r"\s+", " ", titre).strip()

    info_el = card.select_one(".c-cardAnnonce__info__text")
    description = info_el.get_text(" ", strip=True) if info_el else ""

    blob = f"{titre} {description}"

    # prix
    price_el = card.select_one(".c-cardAnnonce__info__price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # terrain (size en hectares → m²)
    size_el = card.select_one(".c-cardAnnonce__location__size")
    surface_terrain = _parse_ha(size_el.get_text(" ", strip=True) if size_el else "")

    # surface habitable depuis titre/texte
    surface = _parse_surface_hab(blob)

    # pièces
    pieces = _parse_pieces(blob)

    # code postal éventuel dans le titre/texte : (49400) ou (49)
    code_postal = _parse_cp(blob, dept)

    # ville : best-effort depuis le titre (segment en MAJUSCULES)
    ville = _parse_ville(titre)

    # id annonce = dernier segment du slug
    id_annonce = parts[-1] if parts else url

    # type de bien
    type_bien = "propriété"
    for rx, label in _TYPE_MAP:
        if rx.search(blob):
            type_bien = label
            break

    # photo
    photos = []
    img = card.select_one(".c-cardAnnonce__image img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src.startswith("//"):
            src = "https:" + src
        if src.startswith("http"):
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "mapropriete",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": (ville or "")[:80],
        "code_postal": code_postal or "",
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "dpe": None,
        "photos": photos,
        "agence": "Ma-Propriété.fr",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text.replace("\xa0", " "))
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    # filtre valeurs aberrantes (un prix immo de prestige >= 50k€)
    return v if v and v >= 1000 else None


def _parse_ha(text: str) -> float | None:
    """'0.4 ha' / '20 ha' → m² ; '2,5 ha' aussi."""
    m = re.search(r"([\d]+(?:[.,]\d+)?)\s*ha", text, re.IGNORECASE)
    if not m:
        return None
    try:
        return round(float(m.group(1).replace(",", ".")) * 10000, 0)
    except ValueError:
        return None


def _parse_surface_hab(text: str) -> float | None:
    """Cherche une surface habitable explicite : '418 m² habitables', '375 m²'."""
    if not text:
        return None
    # priorité aux mentions explicites d'habitable
    m = re.search(
        r"(\d[\d\s\xa0]*)\s*m(?:²|2)?\s*(?:hab|habitable)", text, re.IGNORECASE
    )
    if not m:
        # repli : tout 'NNN m²' plausible (>= 40 m², < 3000 m²)
        for mm in re.finditer(r"(\d[\d\s\xa0]*)\s*m(?:²|2)\b", text, re.IGNORECASE):
            val = _to_int(mm.group(1))
            if val and 40 <= val <= 3000:
                return float(val)
        return None
    val = _to_int(m.group(1))
    if val and 8 <= val <= 3000:
        return float(val)
    return None


def _parse_pieces(text: str) -> int | None:
    m = re.search(r"(\d{1,2})\s*pi[eè]ces", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_cp(text: str, dept: str) -> str | None:
    # CP complet (NNNNN) appartenant au département
    for m in re.finditer(r"\((\d{5})\)", text):
        if m.group(1)[:2] == dept:
            return m.group(1)
    return None


def _parse_ville(titre: str) -> str | None:
    """Best-effort : segment en MAJUSCULES (ville) dans le titre, sinon après 'à'."""
    # tokens en MAJUSCULES (>= 3 lettres), incl. tirets/apostrophes
    m = re.search(r"\b([A-ZÉÈÀÂÊÎÔÛ][A-ZÉÈÀÂÊÎÔÛ' \-]{2,})\b", titre)
    if m:
        cand = m.group(1).strip(" -'")
        # évite de capter des mots génériques tout en maj
        if cand.upper() not in {"XVII", "XVIII", "XIX", "XXE", "XIXE"} and len(cand) >= 3:
            return cand.title()
    m2 = re.search(r"\bà\s+([A-ZÉÈ][\w' \-]+?)(?:\s*\(|,|$)", titre)
    if m2:
        return m2.group(1).strip()
    return None


def _to_int(s: str) -> int | None:
    cleaned = re.sub(r"[\s\xa0]", "", s)
    try:
        return int(cleaned)
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
    print(f"\nTotal Ma-Propriété: {len(biens)} annonces")
    depts = sorted({(b["code_postal"][:2] if b["code_postal"] else b["departement"]) for b in biens})
    print(f"Départements vus : {depts}")
    # contrôle de fuite : code_postal[:2] doit == departement quand CP connu
    leaks = [b for b in biens if b["code_postal"] and b["code_postal"][:2] != b["departement"]]
    print(f"FUITES hors-dept (CP connu) : {len(leaks)}")
    for b in leaks[:10]:
        print(f"  LEAK [{b['code_postal']}] dept={b['departement']} {b['titre'][:50]}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terr {b.get('surface_terrain') or '?'}m²"
            f" — {b['pieces'] or '?'}p — {b['type_bien']} — {b['ville']}"
        )

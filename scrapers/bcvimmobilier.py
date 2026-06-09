"""scrapers/bcvimmobilier.py — BCV Immobilier (agence locale Chartres / Eure-et-Loir)

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /biens.html  (page unique listant tout le stock, pas de pagination)
              Pas de filtre département côté serveur → POST-FILTRE strict sur le
              code département affiché dans la carte (ex: "CHAMPHOL (28)").

Couverture : agence mono-secteur autour de Chartres → quasi 100 % en Eure-et-Loir
             (28), avec quelques biens hors-zone ponctuels (ex: Honfleur 14,
             Conteville 27). Le post-filtre élimine ces fuites.

Cartes : a[data-item="bien"]
  - URL    : href  → biens-{id}-{slug}.html
  - Titre  : .name
  - Loc    : .state  →  "VILLE (NN)"  (code département seul, pas de CP complet)
  - Texte  : .resume (description)
  - Prix   : .price .now  →  "259 000€"  (les locations affichent "…€/ mois" → exclues)
  - Datas  : .datas span  → glyphes FontAwesome :
         année ·  surface habitable ·  terrain ·
         pièces ·  chambres
  - Photo  : img[data-src]  →  uploads/realestate_properties/{id}/...

Type de bien : déduit du titre (maison / appartement / longère / terrain…).
               On ne garde que maisons / propriétés ; appartements exclus par défaut.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.bcvimmobilier.fr"
LIST_URL = f"{BASE_URL}/biens.html"
PHOTOS_PER_CARD = 1  # une seule vignette en liste

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Glyphes FontAwesome utilisés dans .datas
_ICON_YEAR = ""
_ICON_SURFACE = ""
_ICON_TERRAIN = ""
_ICON_PIECES = ""
_ICON_CHAMBRES = ""

# Types de bien (depuis le titre) à conserver
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|corps de ferme|maison de",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|loft|duplex",
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
        try:
            r = await client.get(LIST_URL)
        except Exception as e:
            print(f"[BCV] Erreur réseau : {e}")
            return results

        if r.status_code != 200:
            print(f"[BCV] Statut {r.status_code} sur {LIST_URL}")
            return results

        cards = BeautifulSoup(r.text, "html.parser").select('a[data-item="bien"]')
        seen_ids: set[str] = set()

        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue

            # POST-FILTRE département STRICT (pas de filtre serveur)
            dep = bien["departement"]
            if dep not in departements:
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

    # Comptage par département pour le log
    from collections import Counter

    dist = Counter(b["departement"] for b in results)
    print(f"[BCV] {len(results)} annonces — par dept : {dict(dist)}")
    return results


def _parse_card(card) -> dict | None:
    href = card.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

    # id_annonce depuis biens-{id}-...
    m_id = re.search(r"biens-(\d+)-", href)
    id_annonce = m_id.group(1) if m_id else url

    # Localisation : "VILLE (NN)"
    state_el = card.select_one(".state")
    loc = state_el.get_text(" ", strip=True) if state_el else ""
    ville, dept = _parse_loc(loc)
    if not dept:
        return None

    # Titre
    name_el = card.select_one(".name")
    titre = name_el.get_text(" ", strip=True) if name_el else ""

    # Type de bien (depuis le titre) — exclut appartements/studios/terrains
    if _EXCLUDE_TYPE.search(titre) and not _KEEP_TYPE.search(titre):
        return None
    if not _KEEP_TYPE.search(titre):
        # type ambigu → on exclut par prudence
        return None
    type_bien = _type_from_title(titre)

    # Prix — exclure les locations ("…€/ mois")
    price_el = card.select_one(".price .now")
    price_text = price_el.get_text(" ", strip=True) if price_el else ""
    if re.search(r"mois", price_text, re.IGNORECASE):
        return None
    prix = _parse_price(price_text)

    # Description
    resume_el = card.select_one(".resume")
    description = resume_el.get_text(" ", strip=True) if resume_el else ""

    # Datas (glyphes FontAwesome)
    surface = surface_terrain = pieces = chambres = None
    datas_el = card.select_one(".datas")
    if datas_el:
        for span in datas_el.select("span"):
            txt = span.get_text(" ", strip=True)
            if _ICON_SURFACE in txt:
                surface = _parse_num(txt)
            elif _ICON_TERRAIN in txt:
                surface_terrain = _parse_num(txt)
            elif _ICON_PIECES in txt:
                v = _parse_num(txt)
                pieces = int(v) if v else None
            elif _ICON_CHAMBRES in txt:
                v = _parse_num(txt)
                chambres = int(v) if v else None

    # Photo
    photos = []
    img = card.select_one("img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            src = src if src.startswith("http") else f"{BASE_URL}/{src.lstrip('/')}"
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "bcvimmobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": None,  # liste ne donne que le code dept, pas le CP complet
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "BCV Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'CHAMPHOL (28)' → ('Champhol', '28')"""
    dep = ""
    m = re.search(r"\((\d{2})\)", text)
    if m:
        dep = m.group(1)
    ville = re.sub(r"\s*\(\d{2}\)\s*$", "", text).strip()
    ville = ville.title() if ville.isupper() else ville
    return ville, dep


def _type_from_title(titre: str) -> str:
    t = titre.lower()
    for label, pat in [
        ("longère", r"longere|longère"),
        ("château", r"chateau|château"),
        ("manoir", r"manoir"),
        ("ferme", r"ferme|corps de ferme"),
        ("propriété", r"propriete|propriété"),
        ("moulin", r"moulin"),
        ("demeure", r"demeure"),
        ("maison", r"maison"),
    ]:
        if re.search(pat, t):
            return label
    return "maison"


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_num(text: str) -> float | None:
    """Extrait le premier nombre (gère espaces/insécables et décimale '.' ou ',')."""
    cleaned = text.replace("\xa0", " ")
    m = re.search(r"([\d][\d\s]*(?:[.,]\d+)?)", cleaned)
    if not m:
        return None
    val = m.group(1).replace(" ", "").replace(",", ".")
    try:
        return float(val)
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
    print(f"\nTotal BCV Immobilier: {len(biens)} annonces")
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

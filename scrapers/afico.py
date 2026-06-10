"""scrapers/afico.py — Afico (agence Tours centre, 37, depuis 1960)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress, theme cw-theme, Polylang)
URL : /ventes-maisons-appartements-tours-37/?offre[]=vente
      → une seule page listant toutes les annonces (pas de pagination observée).
      Agence mono-département : toutes les annonces sont en Indre-et-Loire (37),
      autour de Tours. Aucun filtre département serveur (inutile) → POST-FILTRE
      strict sur la commune (dérivée du titre/loc) via CITY_DEPT / un code postal
      reconstruit depuis CITY_CP. Seules les villes du 37 sont conservées.

Cartes : div.iwp__item
  - URL/titre : .iwp__item-text h5 a[href]  → titre "Type – surface – VILLE"
  - Loc       : dernier <p> de .iwp__item-text  → "VILLE" (sans code postal)
  - Prix      : .iwp__price  → "91 800€ FAI*"
  - Overview  : .iwp__item-overview li (svg id) :
        house-plan → surface (m²), bed → chambres, Layer_1 → pièces, bath → sdb
  - Photo     : .iwp__item-image img[src]
  - Type      : déduit du titre ("Maison", "Appartement", "Maison de Ville"…)

Couverture : mono-agence Tours/37. Filtre prix_min/max et surface_min appliqué.
0 fuite hors-zone : seules les communes reconnues du 37 passent le post-filtre.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import unicodedata

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.afico.fr"
LISTING_URL = f"{BASE_URL}/ventes-maisons-appartements-tours-37/?offre%5B%5D=vente"
PHOTOS_PER_CARD = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Communes du secteur Afico (Tours / Indre-et-Loire) → code postal.
# Sert à la fois de filtre département strict (clé = commune normalisée) et à
# reconstruire le code_postal, absent de la vue liste.
CITY_CP: dict[str, str] = {
    "tours": "37000",
    "saint pierre des corps": "37700",
    "la ville aux dames": "37700",
    "saint cyr sur loire": "37540",
    "parcay meslay": "37210",
    "saint avertin": "37550",
    "cinq mars la pile": "37130",
    "joue les tours": "37300",
    "fondettes": "37230",
    "la riche": "37520",
    "chambray les tours": "37170",
    "saint genouph": "37510",
    "ballan mire": "37510",
    "montlouis sur loire": "37270",
    "rochecorbon": "37210",
    "luynes": "37230",
    "vouvray": "37210",
    "notre dame d oe": "37390",
    "la membrolle sur choisille": "37390",
    "veretz": "37270",
    "savonnieres": "37510",
    "esvres": "37320",
    "monnaie": "37380",
    "amboise": "37400",
    "azay le rideau": "37190",
    "langeais": "37130",
    "veigne": "37250",
    "larcay": "37270",
    "berthenay": "37510",
    "druye": "37190",
    "villandry": "37510",
    "nazelles negron": "37530",
    "pocé sur cisse": "37530",
    "poce sur cisse": "37530",
}


def _norm(s: str) -> str:
    """Minuscule, sans accents, espaces simples — pour matcher CITY_CP."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[’'\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Afico ne couvre que le 37 : si 37 n'est pas demandé, rien à faire.
    if "37" not in departements:
        print("[Afico] Département 37 hors critères → 0 annonce")
        return []

    results: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
    ) as client:
        try:
            r = await client.get(LISTING_URL)
        except Exception as e:
            print(f"[Afico] Erreur requête: {e}")
            return []
        if r.status_code != 200:
            print(f"[Afico] HTTP {r.status_code}")
            return []

        cards = BeautifulSoup(r.text, "html.parser").select("div.iwp__item")
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue

            # Post-filtre STRICT : commune reconnue du 37 uniquement (0 fuite)
            if not bien["code_postal"] or bien["code_postal"][:2] not in departements:
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

    print(f"[Afico] {len(results)} annonces (dept 37)")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one(".iwp__item-text h5 a") or card.select_one(
        ".iwp__item-image a"
    )
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    title_el = card.select_one(".iwp__item-text h5 a")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    # Localisation : dernier <p> du bloc texte (ex. "TOURS")
    ville_raw = ""
    text_block = card.select_one(".iwp__item-text")
    if text_block:
        ps = text_block.select("p")
        if ps:
            ville_raw = ps[-1].get_text(" ", strip=True)
    # Repli : dernier segment du titre après le dernier tiret
    if not ville_raw and titre:
        parts = re.split(r"[–\-]", titre)
        ville_raw = parts[-1].strip()

    code_postal = CITY_CP.get(_norm(ville_raw), "")
    ville = ville_raw.title() if ville_raw else ""

    # Type de bien depuis le titre
    type_bien = _parse_type(titre)

    # Prix
    price_el = card.select_one(".iwp__price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Overview : surface / chambres / pièces
    surface = chambres = pieces = None
    for li in card.select(".iwp__item-overview li"):
        svg = li.select_one("svg")
        sid = (svg.get("id") or "") if svg else ""
        sp = li.select_one("span")
        val = sp.get_text(" ", strip=True) if sp else ""
        if "house-plan" in sid:
            surface = _parse_surface(val)
        elif "bed" in sid:
            chambres = _parse_int(val)
        elif sid == "Layer_1":
            pieces = _parse_int(val)
    # Repli surface depuis le titre ("… – 90 m² – …")
    if surface is None:
        surface = _parse_surface(titre)

    # id_annonce : slug d'URL
    slug = href.rstrip("/").split("/")[-1]
    id_annonce = f"afico-{slug}" if slug else url

    # Photo
    photos = []
    for img in card.select(".iwp__item-image img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    if not titre:
        titre = f"{type_bien} {ville}".strip()

    return {
        "source": "afico",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2] if code_postal else "37",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Afico",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_type(titre: str) -> str:
    t = titre.lower()
    if "maison de ville" in t:
        return "maison de ville"
    if "maison" in t:
        return "maison"
    if "appartement" in t or re.search(r"\bt\d\b", t):
        return "appartement"
    if "villa" in t:
        return "villa"
    if "terrain" in t:
        return "terrain"
    if "local" in t or "commerce" in t:
        return "local"
    if "immeuble" in t:
        return "immeuble"
    return "bien"


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text.split("FAI")[0])
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(text: str) -> int | None:
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def _parse_surface(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d+[.,]?\d*)\s*[mM]²", text)
    if m:
        try:
            f = float(m.group(1).replace(",", "."))
            if 5 <= f <= 5000:
                return f
        except ValueError:
            pass
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
    print(f"\nTotal Afico: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

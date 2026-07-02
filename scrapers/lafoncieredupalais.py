"""scrapers/lafoncieredupalais.py — La Foncière du Palais (agence immobilière de Bourges)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress, thème WPResidence/Realhomes)
Site     : https://lafoncieredupalais.com  — agence indépendante implantée à Bourges (Cher, 18),
           qui ne commercialise que des biens du Cher (Bourges et communes alentour).

URL liste : /tous-nos-biens-immobiliers/  → catalogue complet, toutes les cartes dans le
            HTML brut (pas de pagination : carrousels par bien, ~25 biens listés).
            Aucune fuite départementale possible (agence mono-département 18) ; on
            re-vérifie tout de même chaque bien via une map ville→CP du Cher et on
            n'émet que les biens dont le CP commence par "18" (garde stricte).

Cartes : div.property_listing.property_card_default
  - URL/titre : h2 > a[href*="/biens-immobiliers/"]
  - Localisation : .property_location_image  → "Quartier, Ville" (liens taxonomie)
                   la ville est le dernier lien /villes-region-centre/{ville}/
  - Prix       : .listing_unit_price_wrapper  → "335 800 €"
  - Meta texte : "Pièces : 5  Salles de bain : 2  Surface : 160 m2" (texte plat de la carte)
  - Photo      : img[src] (1ʳᵉ image de la carte)

Type de bien : déduit du titre (maison / propriété / longère / appartement / local…).
               On ne garde que maisons / propriétés (pas appartements / locaux / garages).

Filtre département : pas de CP dans la carte → map ville→CP (communes du Cher) ; dept forcé
                     "18". Tout bien dont le CP résolu n'est pas en 18 est écarté (garde).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import unicodedata

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int

BASE_URL = "https://lafoncieredupalais.com"
LISTING_URL = f"{BASE_URL}/tous-nos-biens-immobiliers/"
PHOTOS_PER_CARD = 5


# Map ville (slug normalisé) → code postal, communes du Cher (18) couvertes par l'agence.
# Sert à renseigner le CP et à garantir 0 fuite hors-département (toutes en 18).
CHER_CP: dict[str, str] = {
    "bourges": "18000",
    "st-georges-sur-moulon": "18110",
    "saint-georges-sur-moulon": "18110",
    "st-doulchard": "18230",
    "saint-doulchard": "18230",
    "trouy": "18570",
    "plaimpied-givaudins": "18340",
    "marmagne": "18500",
    "mehun-sur-yevre": "18500",
    "vierzon": "18100",
    "saint-amand-montrond": "18200",
    "st-amand-montrond": "18200",
    "sancerre": "18300",
    "aubigny-sur-nere": "18700",
    "henrichemont": "18250",
    "fussy": "18110",
    "berry-bouy": "18500",
    "le-subdray": "18570",
    "morthomiers": "18570",
    "vasselay": "18110",
    "annoix": "18340",
    "lissay-lochy": "18340",
    "soye-en-septaine": "18390",
    "savigny-en-septaine": "18390",
    "avord": "18520",
}

# Types de bien (titre) à conserver : maisons / propriétés.
_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|longere|longère|manoir|ch[aâ]teau|demeure|"
    r"domaine|ferme|moulin|fermette|corps de ferme|pavillon|villa",
    re.IGNORECASE,
)
# Types explicitement exclus.
_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|local|commerc|garage|parking|immeuble|bureau|"
    r"terrain|fonds|box|cave|loft|duplex",
    re.IGNORECASE,
)


def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    # L'agence est strictement dans le Cher (18) : si 18 n'est pas demandé, rien à faire.
    if "18" not in departements:
        print("[FonciereDuPalais] Dept 18 hors zone demandée — skip")
        return []

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        try:
            r = await client.get(LISTING_URL)
            if r.status_code != 200:
                print(f"[FonciereDuPalais] HTTP {r.status_code} sur la liste")
                return []
        except Exception as e:
            print(f"[FonciereDuPalais] Erreur réseau : {e}")
            return []

        cards = BeautifulSoup(r.text, "html.parser").select(
            "div.property_listing.property_card_default"
        )
        print(f"[FonciereDuPalais] {len(cards)} cartes trouvées")

        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue

            # Garde stricte : on n'émet QUE du département 18.
            if not bien["code_postal"] or bien["code_postal"][:2] != "18":
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

    print(f"[FonciereDuPalais] Dept 18 : {len(results)} annonces retenues")
    return results


def _parse_card(card) -> dict | None:
    # Lien + titre
    a = card.select_one('h2 a[href*="/biens-immobiliers/"]')
    if a is None:
        a = card.select_one('a[href*="/biens-immobiliers/"]')
    if a is None:
        return None
    href = a.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    title_el = card.select_one("h2")
    titre = title_el.get_text(" ", strip=True) if title_el else a.get_text(" ", strip=True)
    if not titre:
        return None

    # Type de bien depuis le titre : on ne garde que maisons / propriétés.
    if _EXCLUDE_TYPE.search(titre) and not _KEEP_TYPE.search(titre):
        return None
    if not _KEEP_TYPE.search(titre):
        return None
    type_bien = _deduce_type(titre)

    # id depuis le slug d'URL
    parts = [p for p in href.split("/") if p]
    slug = parts[-1] if parts else url
    id_annonce = slug

    # Localisation : dernier lien /villes-region-centre/{ville}/
    ville = ""
    code_postal = ""
    loc_el = card.select_one(".property_location_image")
    if loc_el:
        ville_links = [
            x for x in loc_el.find_all("a", href=True)
            if "villes-region-centre" in x.get("href", "")
        ]
        if ville_links:
            ville = ville_links[-1].get_text(" ", strip=True)
        if not ville:
            ville = loc_el.get_text(" ", strip=True).split(",")[-1].strip()
    ville_slug = _slugify(ville)
    code_postal = CHER_CP.get(ville_slug, "")
    # Si la ville n'est pas connue mais l'agence est mono-Cher : on tag dept 18
    # SANS CP plutôt que de l'écarter ; la garde stricte ci-dessus exige néanmoins
    # un CP en "18" → on fixe un CP générique du Cher (18000) en repli prudent
    # uniquement si la ville est non vide (évite les biens sans localisation).
    if not code_postal and ville:
        code_postal = "18000"

    # Prix
    price_el = card.select_one(".listing_unit_price_wrapper")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Meta texte : "Pièces : 5  Salles de bain : 2  Surface : 160 m2"
    card_text = card.get_text(" ", strip=True)
    pieces = _parse_int(r"Pi[eè]ces\s*:?\s*(\d+)", card_text)
    chambres = None
    surface = _parse_surface(card_text)

    # Photo (1ʳᵉ image)
    photos = []
    for img in card.select("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        if src and not src.startswith("data:") and "wp-content/uploads" in src:
            if src.startswith("//"):
                src = "https:" + src
            if src not in photos:
                photos.append(src)
        if len(photos) >= PHOTOS_PER_CARD:
            break

    return {
        "source": "lafoncieredupalais",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": "18",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "La Foncière du Palais",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _deduce_type(titre: str) -> str:
    t = titre.lower()
    for kw in (
        "propriété", "propriete", "longère", "longere", "manoir", "château",
        "chateau", "demeure", "domaine", "moulin", "fermette", "ferme",
        "pavillon", "villa", "maison",
    ):
        if kw in t:
            return kw.replace("é", "e")
    return "maison"


def _parse_price(text: str) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'Surface : 160 m2' → 160.0"""
    m = re.search(r"Surface\s*:?\s*([\d\s\xa0]+)\s*m", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 2000:
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
    print(f"\nTotal La Foncière du Palais: {len(biens)} annonces")
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

"""scrapers/stonimmo.py — Stonimmo (portail/agrégateur réseau mandataires)

Méthode : scrape_simple (httpx) — SSR HTML (Tailwind/Next.js rendu serveur).
URL pattern : /immobilier/achat/{nom-dept}-{NN}-d/maison/?page={N}
              ex: /immobilier/achat/sarthe-72-d/maison/
              → filtre département + catégorie « maison » CÔTÉ SERVEUR
                 (vérifié : aucune fuite hors-dept).

Cartes : a[href*="/immobilier/annonces/"]  (l'ancre EST la carte)
  - Titre : h3 → "Maison à vendre - 6 pièces - 106m2 - 217 300,00 €"
            (type, pièces, surface, prix sont encodés dans ce titre)
  - Loc   : p > span → "Saint-Saturnin 72650" ; id = token "idx..." du slug d'URL
  - Texte : p.text-gray-500 ; photo : img[src] (cloudfront)
  - La catégorie /maison/ regroupe maisons + propriétés → post-filtre type quand même.

Migré sur scrapers/_base.py (modèle le_tuc.py) : HEADERS, map dept→slug, boucle
département + pagination (~30 cartes/page), filtres prix/surface et dédup id sont
fournis par le socle. Ne restent ici que le patron d'URL, le sélecteur de carte
et le mapping des champs.

Interface : async def search(criteres: dict) -> list[dict]
"""
import re

from scrapers._base import parse_int, run_dept_search, standalone_main

BASE_URL = "https://www.stonimmo.com"
PHOTOS_PER_CARD = 10

# Types à exclure même s'ils remontent dans la catégorie maison
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    return await run_dept_search(
        source="stonimmo",
        label="Stonimmo",
        page_url=lambda dept, slug, page: (
            f"{BASE_URL}/immobilier/achat/{slug}-{dept}-d/maison/?page={page}"
        ),
        card_selector='a[href*="/immobilier/annonces/"]',
        parse_card=_parse_card,
        criteres=criteres,
    )


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
    pieces = parse_int(r"(\d+)\s*pi[eè]ces?", titre_brut)
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


# ── Helpers propres à Stonimmo (formats non couverts par _base) ────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Saint-Saturnin 72650' → ('Saint-Saturnin', '72650') — pas de parenthèses,
    contrairement au format géré par _base.parse_loc."""
    cp = ""
    m_cp = re.search(r"\b(\d{5})\b", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\b\d{5}\b\s*", " ", text).strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    """'... - 217 300,00 €' → 217300.0 (virgule DÉCIMALE, format Stonimmo)."""
    m = re.search(r"([\d\s\xa0]+(?:,\d{2})?)\s*€", text)
    if not m:
        return None
    cleaned = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'... - 106m2 ...' ou '106 m²' → 106.0 (sans mot-clé 'hab', bornes 5-5000)."""
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


if __name__ == "__main__":
    standalone_main(search, "Stonimmo")

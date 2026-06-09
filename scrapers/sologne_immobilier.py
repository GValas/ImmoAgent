"""scrapers/sologne_immobilier.py — Sologne Immobilier (agence de niche Sologne)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress thème RealHomes, classes rh_*).
URL pattern : /les-proprietes/[page/N/]   (listing national de l'agence, ~15 biens).
              Pas de filtre dept côté serveur → on scrape tout le listing et on
              POST-FILTRE strict sur le département (parsé du titre/URL). 0 fuite.

Cartes : article.rh_prop_card
  - Titre : .rh_prop_card__details (h2/a)  →  "Maison ... – Romorantin (41)"
  - URL   : a[href*="/propriete/"]
  - Excerpt: .rh_prop_card__excerpt
  - Méta  : .rh_prop_card__meta_wrap  →  "Chambres 4 Salles de bain 2 Surface 208 m²"
  - Prix  : .rh_prop_card__price  →  "399,000€"  (virgule = séparateur de milliers)
  - Photo : figure img[src]

Département : pas de code postal exposé dans la liste. On le déduit de :
             1) parenthèse "(NN)" dans le titre/excerpt,
             2) suffixe "-NN" du slug d'URL.
             Un bien sans département identifiable est ÉCARTÉ (prudence → 0 fuite).
             code_postal laissé vide (NN00 non fiable) ; geolocate gère via la ville.

Type de bien : RealHomes mélange maison/propriété/longère/ferme + qq appartements.
               On exclut appartement / terrain seul.

Couverture : Sologne et alentours (41, 45, 18, 37) — propriétés rurales, longères,
             fermes, domaines de chasse : profil très proche des critères cibles.
             dernier_test 2026-06-09.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.sologne-immobilier.com"
LISTING = "/les-proprietes/"
MAX_PAGES = 6
PHOTOS_PER_CARD = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_KEEP_TYPE = re.compile(
    r"maison|villa|propriete|propriété|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|corps de ferme|pavillon|grange|"
    r"habitation|exploitation|gite|gîte",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain (?:a |à )?b[aâ]tir|terrain constructible|local commercial|"
    r"commerce|garage|parking|immeuble de rapport|bureau|fonds de commerce",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = BASE_URL + LISTING + (f"page/{page}/" if page > 1 else "")
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Sologne] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("article.rh_prop_card")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # Post-filtre dept STRICT (pas de filtre serveur)
                if bien["departement"] not in departements:
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
                new_on_page += 1

            await asyncio.sleep(0.5)

    # Récapitulatif par dept
    from collections import Counter

    for d, n in sorted(Counter(b["departement"] for b in results).items()):
        print(f"[Sologne] Dept {d}: {n} annonces")

    return results


def _parse_card(card) -> dict | None:
    link = card.select_one('a[href*="/propriete/"]')
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    details_el = card.select_one(".rh_prop_card__details")
    titre = ""
    if details_el:
        h = details_el.select_one("h2, h3, a")
        titre = (h.get_text(" ", strip=True) if h else
                 details_el.get_text(" ", strip=True))
    titre = re.sub(r"\s+", " ", titre).strip()

    excerpt_el = card.select_one(".rh_prop_card__excerpt")
    description = excerpt_el.get_text(" ", strip=True) if excerpt_el else ""

    # Type de bien (depuis titre)
    base_text = titre or description
    if _EXCLUDE_TYPE.search(base_text):
        return None
    if not _KEEP_TYPE.search(base_text):
        return None
    type_bien = _type_label(base_text)

    # Département : (NN) dans titre/excerpt, sinon suffixe -NN du slug
    dept = _parse_dept(titre) or _parse_dept(description) or _dept_from_url(href)
    if not dept:
        return None

    # Ville : "– {Ville} (NN)" ou "Secteur {Ville}" dans le titre
    ville = _parse_ville(titre)

    # Méta : Chambres / Surface
    meta_el = card.select_one(".rh_prop_card__meta_wrap")
    meta = meta_el.get_text(" ", strip=True) if meta_el else ""
    chambres = _parse_int(r"Chambres\s*(\d+)", meta)
    surface = _parse_surface(meta) or _parse_surface(titre)

    # Terrain : depuis titre/excerpt ("sur 14 hectares", "3 000 m² de terrain")
    surface_terrain = _parse_terrain(titre) or _parse_terrain(description)

    # Prix : "399,000€" (virgule = milliers)
    price_el = card.select_one(".rh_prop_card__price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Photos
    photos = []
    for img in card.select("figure img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    # id_annonce : dernier segment du slug
    parts = [p for p in href.rstrip("/").split("/") if p]
    id_annonce = parts[-1] if parts else url

    return {
        "source": "sologne_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Sologne Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _type_label(text: str) -> str:
    m = _KEEP_TYPE.search(text)
    return m.group(0).lower() if m else "propriete"


def _parse_dept(text: str) -> str | None:
    """Parenthèse '(NN)' = code département à 2 chiffres."""
    if not text:
        return None
    for m in re.finditer(r"\((\d{2})\)", text):
        return m.group(1)
    return None


def _dept_from_url(href: str) -> str | None:
    """Suffixe '-NN' du slug : '.../propriete-rurale-...-37/' → '37'."""
    slug = href.rstrip("/").split("/")[-1]
    m = re.search(r"-(\d{2})$", slug)
    return m.group(1) if m else None


def _parse_ville(titre: str) -> str:
    """Récupère la ville après '–' ou 'Secteur', avant la parenthèse dept."""
    t = re.sub(r"\s*\(\d{2}\)\s*", "", titre)
    m = re.search(r"(?:Secteur|–|-)\s+([A-ZÀ-Ý][\w'’\- ]+?)\s*$", t)
    if m:
        ville = m.group(1).strip()
        ville = re.sub(r"^Secteur\s+", "", ville, flags=re.IGNORECASE).strip()
        return ville
    return ""


def _parse_price(text: str) -> float | None:
    # "399,000€" → 399000 ; "1,250,000 €" → 1250000
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_surface(text: str) -> float | None:
    """'... Surface 208 m²' → 208.0 (surface habitable)."""
    if not text:
        return None
    m = re.search(r"Surface\s*([\d\s\xa0]+(?:[.,]\d+)?)\s*m²?", text, re.IGNORECASE)
    if not m:
        # secours : 'de 208 m²' / '208 m² hab'
        m = re.search(r"(?:de\s+)?([\d\s\xa0]{2,}(?:[.,]\d+)?)\s*m²?\s*(?:hab|habitable)?", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
        try:
            f = float(val)
            if 8 <= f <= 5000:
                return f
        except ValueError:
            pass
    return None


def _parse_terrain(text: str) -> float | None:
    """'sur 14 hectares' → 140000 ; '3 000 m² de terrain' → 3000."""
    if not text:
        return None
    m = re.search(r"([\d\s\xa0]+(?:[.,]\d+)?)\s*(?:ha|hectares?)", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
        try:
            return round(float(val) * 10000)
        except ValueError:
            pass
    m = re.search(r"([\d\s\xa0]{2,})\s*m²?\s*de\s+terrain", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            return float(val)
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
    print(f"\nTotal Sologne Immobilier: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

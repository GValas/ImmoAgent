"""scrapers/comptoir_immo_france.py — Comptoir Immobilier de France (cif-immo.com)

Réseau de mandataires national (France, Corse, Espagne).

Méthode : scrape_simple (httpx) — SSR HTML (générateur « Levant »/bObcat).
URL pattern : /vente/{page}?prices[min]=..&prices[max]=..&surface[min]=..
              → le site N'A PAS de filtre département serveur fiable
              (les params dept/localisation sont ignorés), MAIS il accepte des
              filtres prix/surface côté serveur via query string (vérifié :
              1239 annonces → 151 avec prices[300000-600000]+surface[min]=150).
              On scrape donc le national pré-filtré prix/surface, puis on
              POST-FILTRE strictement par département sur le code postal.

Filtre département : ROBUSTE par code postal. Chaque carte expose
  « Ville (CODEPOSTAL) » dans .title-v1__part1 → CP[:2] comparé aux départements
  cibles. (Le numéro et le nom du dept apparaissent aussi dans le slug detail,
  ex. /vente/411-evreux/ferme/100651-...-dpt-eure-27-... ; on l'utilise en
  secours si le CP carte manque.)

Cartes : article.item
  - URL    : a.links-group__link--drawing[href]  → /vente/{geoid}-{ville}/{type}/{id}-slug
  - Ville+CP : .title-v1__part1                  → "Bolquère (66210)"
  - Titre  : attribut title du lien, ou .item__block--title
  - Type/pieces/chambres/surface : .item__block--title → "Maison 5 pièce(s) 4 chambre(s) 91.7 m²"
  - Type (segment URL) : maison / propriete / ferme / appartement / immeuble...
  - Terrain : .item__options  → "1 1 387 m²" (valeur juste avant le dernier m²)
  - Prix   : .item__price                         → "810 000 €"
  - Photos : .item__media-swiper-wrapper picture img[src] (cif-france.staticlbi.com)

Type de bien : déduit du segment d'URL ; on ne garde que maisons/propriétés/
               fermes/longères/manoirs etc. (exclut appartement/immeuble/terrain…).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.cif-immo.com"
MAX_PAGES = 20
PHOTOS_PER_CARD = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Codes département cibles → nom (pour secours via slug detail).
DEPT_NOMS: dict[str, str] = {
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

# Types de bien (segment d'URL) à conserver : maisons / propriétés / fermes...
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|bastide|"
    r"maison-de-village|maison-de-maitre|maison-de-maître|grange|chalet",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"viager|cave|box|studio",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)
    dept_set = set(departements)

    # Filtres serveur prix/surface (query string GET, vérifiés fonctionnels)
    params: list[str] = []
    if prix_min:
        params.append(f"prices[min]={int(prix_min)}")
    if prix_max:
        params.append(f"prices[max]={int(prix_max)}")
    if surface_min:
        params.append(f"surface[min]={int(surface_min)}")
    qs = ("?" + "&".join(params)) if params else ""

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/vente/{page}{qs}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[ComptoirImmoFrance] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("article.item")
            if not cards:
                break

            for card in cards:
                try:
                    bien = _parse_card(card, dept_set)
                except Exception:
                    continue
                if not bien:
                    continue

                # Post-filtre département STRICT (0 fuite)
                cp = bien["code_postal"] or ""
                dept = cp[:2] if cp else bien["departement"]
                if dept not in dept_set:
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

            await asyncio.sleep(0.5)

    print(f"[ComptoirImmoFrance] Total après post-filtre dept : {len(results)} annonces")
    return results


def _parse_card(card, dept_set: set[str]) -> dict | None:
    link = card.select_one("a.links-group__link--drawing") or card.select_one(
        "a[href^='/vente/']"
    )
    href = link.get("href", "") if link else ""
    if not href or "/vente/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL : /vente/{geoid}-{ville}/{type}/{id}-slug
    parts = [p for p in href.split("/") if p]
    type_seg = parts[2] if len(parts) > 2 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean) and not _KEEP_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        return None
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # id_annonce : id numérique du dernier segment (ex. 100651-a-vendre-...)
    id_annonce = url
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_annonce = m.group(1)

    # Localisation : "Ville (CODEPOSTAL)"
    loc_el = card.select_one(".title-v1__part1") or card.select_one(".item__block--city")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)

    # Secours dept : numéro dans le slug detail (... -dpt-eure-27 / -78 ...)
    departement = code_postal[:2] if code_postal else ""
    if not departement:
        for d in dept_set:
            nom = DEPT_NOMS.get(d, "")
            if re.search(rf"-{d}(?:-|$|\b)", href) or (nom and nom in href):
                departement = d
                break

    # Titre : attribut title du lien (préfixé "Voir le bien "), sinon bloc titre
    titre = (link.get("title", "") if link else "").strip()
    titre = re.sub(r"^Voir le bien\s+", "", titre, flags=re.IGNORECASE).strip()
    title_blk = card.select_one(".item__block--title")
    title_blk_text = (
        re.sub(r"\s+", " ", title_blk.get_text(" ", strip=True))
        if title_blk
        else ""
    )
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip() or title_blk_text

    # Type/pieces/chambres/surface depuis "Maison 5 pièce(s) 4 chambre(s) 91.7 m²"
    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", title_blk_text)
    chambres = _parse_int(r"(\d+)\s*chambre", title_blk_text)
    surface = _parse_surface(title_blk_text)

    # Terrain depuis .item__options : l'option dont le texte contient « m² ».
    surface_terrain = None
    opt_el = card.select_one(".item__options")
    if opt_el:
        for o in opt_el.select(".option"):
            otxt = re.sub(r"\s+", " ", o.get_text(" ", strip=True))
            if "m²" in otxt:
                surface_terrain = _parse_terrain(otxt)
                break

    # Prix
    price_el = card.select_one(".item__price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Photos
    photos: list[str] = []
    for img in card.select(".item__media-swiper-wrapper img, picture.media-js img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "comptoir_immo_france",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": title_blk_text[:1200],
        "departement": departement,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Comptoir Immobilier de France",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Bolquère (66210)' → ('Bolquère', '66210')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_surface(text: str) -> float | None:
    """Dernier 'NNN[.N] m²' du bloc titre = surface habitable."""
    matches = re.findall(r"([\d]+(?:[.,]\d+)?)\s*m²", text)
    if matches:
        val = matches[-1].replace(",", ".")
        try:
            f = float(val)
            if 8 <= f <= 5000:
                return f
        except ValueError:
            pass
    return None


def _parse_terrain(text: str) -> float | None:
    """'1 1 387 m²' → 387.0 (nombre juste avant le 1er m²)."""
    m = re.search(r"([\d][\d\s\xa0]*)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if f >= 10:
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
    print(f"\nTotal Comptoir Immobilier de France: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

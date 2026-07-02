"""scrapers/transaxia.py — Transaxia Immobilier (réseau ~100 agences Centre / Berry)

Méthode : scrape_simple (httpx) — SSR PHP custom.
Couverture : Centre-Val de Loire / Berry / Bourbonnais (18, 36, 37, 45, 58, 23, 03…).
            ~1300 annonces vente au total. Aucune implantation en Sarthe/Anjou/Maine
            (72, 28, 89, 49, 53, 41 → 0 résultat, normal).

Listing : /recherche?type_offre=2&code_postal={NN}&page={N}
  - type_offre=2 → ventes
  - code_postal={NN} : filtre serveur FLOU (remonte le dept demandé en tête mais
    laisse fuiter les départements voisins, et pour un dept non couvert il retombe
    sur une liste par défaut). → on POST-FILTRE STRICTEMENT par le (NN) affiché.
  - page : pagination 0-indexée, 12 cartes/page.

ATTENTION : le DOM contient des cartes .trend-item AUSSI dans la sidebar
(.list-sidebar) et le footer (.footer-links) — ce sont des "coups de cœur",
PAS des résultats. On scope donc à `.listing-inner` pour ne lire que les vrais
résultats, puis on filtre par département.

Cartes (.listing-inner .trend-item) :
  - URL   : <a href=".../immobilier-{ville-slug}/{type}-{...}-{id}">
  - Titre : h4  →  "Maison / Pavillon 785m²"  (type + surface habitable)
  - Loc   : .entry-author  →  "Sancoins (18) 499 800 €"  (ville + (dept) + prix)
  - Prix  : .entry-author span.theme  →  "499 800 €"
  - Desc  : p.mb-1  (accroche courte)
  - Specs : ul li  →  "785m²" | "22 pièces" | "16 chambres"
  - Photo : .trend-image img[src]

Type de bien : déduit du titre / slug. On garde maisons, fermettes, longères,
               propriétés, châteaux, manoirs, moulins, demeures… On exclut
               terrains, appartements, immeubles, locaux commerciaux, parkings.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://transaxia.fr"
MAX_PAGES = 60          # plafond de sécurité (le filtre flou disperse un dept sur
                        # beaucoup de pages ; ~111 pages pour l'inventaire complet)
PHOTOS_PER_CARD = 1     # 1 photo de couverture sur la liste


# Types de bien (titre/slug) à conserver
_KEEP_TYPE = re.compile(
    r"maison|pavillon|propriete|propriété|villa|fermette|ferme|longere|longère|"
    r"manoir|chateau|château|moulin|demeure|domaine|mas\b|gite|gîte|"
    r"corps[- ]de[- ]ferme|maison[- ]de[- ]village|grange",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"terrain|appartement|immeuble|local|commercial|commerce|garage|parking|"
    r"bureau|fonds|entrep[oô]t|hangar|immobilier[- ]d.entreprise",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            try:
                biens = await _scrape_dept(
                    client, dept, prix_max, prix_min, surface_min, seen_ids
                )
                results.extend(biens)
                print(f"[Transaxia] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Transaxia] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.5)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
    seen_ids: set[str],
) -> list[dict]:
    """Scrape les ventes d'un département.

    On interroge ?code_postal={NN} (qui remonte le dept en tête) puis on
    POST-FILTRE strictement par le (NN) affiché sur la carte. On pagine tant
    qu'on rencontre encore des biens du département cible (avec une petite
    tolérance de pages "creuses" car le filtre flou intercale d'autres depts).
    """
    biens: list[dict] = []
    empty_streak = 0  # pages consécutives sans aucun bien du dept cible

    for page in range(0, MAX_PAGES):
        url = f"{BASE_URL}/recherche?type_offre=2&code_postal={dept}&page={page}"
        r = await client.get(url)
        if r.status_code != 200:
            break

        soup = BeautifulSoup(r.text, "html.parser")
        inner = soup.select_one(".listing-inner")
        if inner is None:
            break
        cards = inner.select(".trend-item")
        if not cards:
            break

        matched_on_page = 0
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue

            # FILTRE DÉPARTEMENT STRICT (le filtre serveur est flou)
            if bien["departement"] != dept:
                continue
            matched_on_page += 1

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

        # Arrêt : 6 pages d'affilée sans aucun bien du dept cible (le filtre flou
        # intercale d'autres départements, donc on tolère des pages creuses).
        if matched_on_page == 0:
            empty_streak += 1
            if empty_streak >= 6:
                break
        else:
            empty_streak = 0

        await asyncio.sleep(0.4)

    return biens


def _parse_card(card) -> dict | None:
    # Lien fiche : premier <a> vers /immobilier-...
    href = ""
    for a in card.select("a"):
        h = a.get("href", "")
        if "/immobilier-" in h:
            href = h
            break
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + "/" + href.lstrip("/")

    # id annonce : suffixe numérique du slug final
    id_annonce = ""
    m_id = re.search(r"-(\d+)(?:[/#?].*)?$", url)
    if m_id:
        id_annonce = m_id.group(1)
    if not id_annonce:
        id_annonce = url

    # Titre : "Maison / Pavillon 785m²"
    h4 = card.select_one("h4")
    titre = h4.get_text(" ", strip=True) if h4 else ""

    # Type de bien (titre prioritaire, sinon slug)
    type_seg = url.split("/")[-1]
    type_source = f"{titre} {type_seg}"
    if _EXCLUDE_TYPE.search(type_source) and not _KEEP_TYPE.search(titre):
        return None
    if not _KEEP_TYPE.search(type_source):
        return None
    type_bien = _type_label(type_source)

    # Localisation + prix : "Sancoins (18) 499 800 €"
    au = card.select_one(".entry-author")
    au_text = au.get_text(" ", strip=True) if au else ""
    ville, dept = _parse_loc(au_text)
    if not dept:
        return None

    prix = None
    price_el = au.select_one("span.theme") if au else None
    if price_el:
        prix = _parse_num(price_el.get_text(" ", strip=True))
    if prix is None:
        # secours : tout montant € dans le bloc auteur
        m_p = re.search(r"([\d\s\xa0]+)\s*€", au_text)
        if m_p:
            prix = _parse_num(m_p.group(1))

    # Specs : <ul><li>785m²</li><li>22 pièces</li><li>16 chambres</li>
    surface = None
    pieces = None
    chambres = None
    ul = card.select_one("ul")
    if ul:
        spec_text = ul.get_text(" | ", strip=True)
        m_s = re.search(r"([\d\s\xa0,\.]+)\s*m²", spec_text)
        if m_s:
            surface = _parse_num(m_s.group(1))
        m_p = re.search(r"(\d+)\s*pi[eè]ce", spec_text, re.IGNORECASE)
        if m_p:
            pieces = int(m_p.group(1))
        m_c = re.search(r"(\d+)\s*chambre", spec_text, re.IGNORECASE)
        if m_c:
            chambres = int(m_c.group(1))

    # Surface en secours depuis le titre ("... 785m²")
    if surface is None and titre:
        m_st = re.search(r"([\d\s\xa0,\.]+)\s*m²", titre)
        if m_st:
            surface = _parse_num(m_st.group(1))

    # Description / accroche
    desc_el = card.select_one("p.mb-1")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Code postal : non exposé sur la liste (seul le dept l'est). On laisse vide,
    # le département est la seule info de localisation fiable côté liste.
    code_postal = ""

    # Photo de couverture
    photos = []
    img = card.select_one(".trend-image img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src.startswith("//"):
            src = "https:" + src
        if src.startswith("http") and "cdc" not in src.split("/")[-1]:
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "transaxia",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150] if titre else f"{type_bien.title()} {ville}".strip(),
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "dpe": None,
        "photos": photos,
        "agence": "Transaxia",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Sancoins (18) 499 800 €' → ('Sancoins', '18')

    Le département est entre parenthèses (2 chiffres). On coupe la ville avant
    la parenthèse et on ignore tout ce qui suit (le prix)."""
    m = re.search(r"\((\d{2,3})\)", text)
    dept = ""
    if m:
        dept = m.group(1)
        # normalise sur 2 chiffres (jamais d'arrondissement ici)
        if len(dept) == 3:
            dept = dept[:2]
        else:
            dept = dept.zfill(2)
    ville = text.split("(")[0].strip()
    return ville, dept


def _type_label(text: str) -> str:
    t = text.lower()
    if re.search(r"chateau|château", t):
        return "château"
    if "manoir" in t:
        return "manoir"
    if re.search(r"longere|longère|fermette|ferme|corps[- ]de[- ]ferme", t):
        return "fermette"
    if "moulin" in t:
        return "moulin"
    if re.search(r"propriete|propriété|demeure|domaine", t):
        return "propriété"
    if "villa" in t:
        return "villa"
    return "maison"


def _parse_num(text: str) -> float | None:
    """'499 800 €' / '785m²' / '1 700 000' → float"""
    cleaned = text.replace("\xa0", " ")
    cleaned = re.sub(r"[^\d,\.]", "", cleaned.replace(" ", ""))
    cleaned = cleaned.replace(",", ".")
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned) if cleaned else None
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
    print(f"\nTotal Transaxia (depts cibles): {len(biens)} annonces")
    from collections import Counter
    dist = Counter(b["departement"] for b in biens)
    print(f"Répartition par dept : {dict(sorted(dist.items()))}")
    leaks = [b for b in biens if b["departement"] not in
             [str(d).zfill(2) for d in criteres.departements]]
    print(f"FUITES hors-dept : {len(leaks)}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:50]}"
            f" — {b['prix']}€ — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p — {b['ville']} ({b['type_bien']})"
        )

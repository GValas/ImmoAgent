"""scrapers/immobilier_entre_particuliers.py — Immobilier Entre Particuliers (P2P)

⚠️ À NE PAS confondre avec :
  - immo_entre_particuliers.py  → immo-entre-particuliers.com
  - entreparticuliers.py        → entreparticuliers.com
Ce scraper cible immobilier-entre-particuliers.fr (portail zone-annonces).

Méthode : scrape_simple (httpx) — SSR HTML (Apache, pas de Cloudflare/JS).

Filtre département CÔTÉ SERVEUR via un TOKEN d'ID de localisation :
  Les slugs nus /vente-{nom-dept} NE filtrent PAS (fuite nationale — vérifié).
  Le vrai filtre serveur passe par l'URL canonique se terminant par -g{id} :
    /vente-maison-{nom-dept}-{NN}-g{ID}[?page=N]
    (ex: /vente-maison-loiret-45-g36768)
  {ID} = identifiant interne du département, résolu DYNAMIQUEMENT via
  l'endpoint d'autocomplétion /ajax/locations.php?q={nom} (robuste : pas de
  table d'IDs en dur). On filtre sur le type "maison" côté serveur, puis on
  re-vérifie STRICTEMENT code_postal[:2] == dept (0 fuite).

Pagination : ?page=N (12 cartes/page). Stop dès qu'une page n'a pas de carte
  ou aucun bien nouveau.

Cartes : div.row.search-ad.ad  (id="ad-{id}")
  - URL    : a[href*=annonce]  → /…/annonce-immobiliere-particulier-{id}.html
  - Loc    : p > strong         →  "Bouzy la Forêt (45460)"   (ville + CP)
  - Titre  : h3                 →  "Vente Maison 5 pièces 150 m2 Ville (45460)"
  - Desc   : <p> (après le strong) → texte court
  - Specs  : ul.search-main-features li (span étiquette + valeur)
             Pièces / Chambres / Salle de bains / Surface
  - Prix   : div.price > span    →  "300 000 €"
  - Photo  : div.thumbnail-img img[src]

Particularités : annonces de particuliers, DPE/terrain non exposés sur la carte
  (→ dpe=None, surface_terrain=None). Surface habitable et pièces présentes.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.immobilier-entre-particuliers.fr"
LOCATIONS_AJAX = BASE_URL + "/ajax/locations.php"
MAX_PAGES = 15
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Referer": BASE_URL + "/vente",
}

# Nom de département (slug d'autocomplétion) → utilisé pour résoudre l'ID serveur
# et pour construire l'URL canonique. Le CP (2 chiffres) sert de garde-fou.
DEPT_NAMES: dict[str, str] = {
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

_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|long[eè]re|manoir|chateau|château|"
    r"moulin|demeure|domaine|mas|g[iî]te|corps.de.ferme|maison.de.village",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|studio|loft|chalet",
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
            name = DEPT_NAMES.get(dept)
            if not name:
                continue
            try:
                loc_id = await _resolve_location_id(client, name, dept)
                if not loc_id:
                    print(f"[ImmobilierEntreParticuliers] Dept {dept}: id introuvable")
                    continue
                biens = await _scrape_dept(
                    client, dept, name, loc_id, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(
                    f"[ImmobilierEntreParticuliers] Dept {dept}: {len(biens)} annonces"
                )
            except Exception as e:
                print(f"[ImmobilierEntreParticuliers] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _resolve_location_id(
    client: httpx.AsyncClient, name: str, dept: str
) -> str | None:
    """Résout l'ID serveur du département via /ajax/locations.php."""
    headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
    r = await client.get(LOCATIONS_AJAX, params={"q": name}, headers=headers)
    if r.status_code != 200:
        return None
    try:
        items = r.json().get("items", [])
    except Exception:
        return None
    # On veut l'entrée "département" : cp == code à 2 chiffres exactement.
    for it in items:
        if str(it.get("cp", "")) == dept:
            return str(it.get("id"))
    return None


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    name: str,
    loc_id: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    base = f"{BASE_URL}/vente-maison-{name}-{dept}-g{loc_id}"
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = base if page == 1 else f"{base}?page={page}"
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select("div.search-ad.ad")
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

            # Garde-fou STRICT : uniquement le département cible (0 fuite)
            if not bien["code_postal"] or bien["code_postal"][:2] != dept:
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

        if new_on_page == 0:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a[href*=annonce]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce : id="ad-{id}" ou segment du href
    id_annonce = ""
    card_id = card.get("id", "")
    m_id = re.match(r"ad-(\d+)", card_id)
    if m_id:
        id_annonce = m_id.group(1)
    if not id_annonce:
        m = re.search(r"annonce-immobiliere-particulier-(\d+)", href)
        if m:
            id_annonce = m.group(1)
    if not id_annonce:
        id_annonce = url

    # Localisation : <p><strong>Ville (CP)</strong> ...</p>
    strong = card.select_one("p strong")
    loc = strong.get_text(" ", strip=True) if strong else ""
    ville, code_postal = _parse_loc(loc)

    # Titre (h3)
    h3 = card.select_one("h3")
    titre = _clean_text(h3.get_text(" ", strip=True)) if h3 else ""

    # Type : on n'accepte que maisons / propriétés
    if _EXCLUDE_TYPE.search(titre) and not _KEEP_TYPE.search(titre):
        return None
    m_type = _KEEP_TYPE.search(titre)
    if not m_type:
        return None
    type_bien = m_type.group(0).lower()

    # Description : texte du <p> après le strong
    desc = ""
    p_loc = strong.find_parent("p") if strong else None
    if p_loc:
        full = _clean_text(p_loc.get_text(" ", strip=True))
        # retire le préfixe "Ville (CP) ."
        desc = re.sub(r"^.*?\(\d{5}\)\s*\.?\s*", "", full).strip()

    # Prix
    price_el = card.select_one("div.price span") or card.select_one(".price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Specs : ul.search-main-features
    pieces = chambres = None
    surface = None
    feats = card.select_one("ul.search-main-features")
    if feats:
        for li in feats.select("li"):
            sp = li.select_one("span")
            label = sp.get_text(strip=True).lower() if sp else ""
            val = li.get_text(" ", strip=True)
            num = _first_num(val)
            if "pièce" in label or "piece" in label:
                pieces = int(num) if num is not None else pieces
            elif "chambre" in label:
                chambres = int(num) if num is not None else chambres
            elif "surface" in label:
                surface = num if (num and 8 <= num <= 2000) else surface

    # Photos
    photos = []
    for img in card.select("img"):
        src = img.get("data-src") or img.get("src") or ""
        if not src or src.startswith("data:"):
            continue
        if src.startswith("//"):
            src = "https:" + src
        photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "immobilier_entre_particuliers",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": desc[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,  # non exposé sur la carte
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Immobilier Entre Particuliers",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clean_text(text: str) -> str:
    """Recolle les 'm 2' (du <sup>2</sup>) en 'm²' et compacte les espaces."""
    text = re.sub(r"m\s*2\b", "m²", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_loc(text: str) -> tuple[str, str]:
    """'Bouzy la Forêt (45460)' → ('Bouzy la Forêt', '45460')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", re.sub(r"plus de.*$", "", text, flags=re.I))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _first_num(text: str) -> float | None:
    m = re.search(r"(\d[\d\s\xa0]*)", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
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
    print(f"\nTotal Immobilier Entre Particuliers: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p/{b['chambres'] or '?'}ch"
            f" — {b['type_bien']} — {b['ville']}"
        )

"""scrapers/mdi_anjoumaine.py — MDI Anjou-Maine (agence locale Mayenne/Anjou/Sarthe)

Méthode : scrape_simple (httpx) — SSR WordPress + Elementor (loop-item).

Listing : https://mdi-anjoumaine.fr/bien/
  - Filtre offre CÔTÉ SERVEUR : ?ads_filters_type_offre=Vente
    (vérifié : ne renvoie que des biens "Vente, …", aucune Location ne fuit).
  - Pagination : /bien/page/{N}/?ads_filters_type_offre=Vente  (s'arrête quand 0 carte).
  - Inventaire : ~72 annonces "Vente" (toutes catégories confondues).

Cartes : div.bien.e-loop-item  (id="post-NNNNNN")
  - URL    : a.ann[href]  → /bien/{slug}/
  - Champs : <p|h2 class="elementor-heading-title"> successifs :
      [0] titre, [1] "Vente, {Type}", [..] badge éventuel (Nouveauté/Exclusivité),
      [-3] "NN m² | N pièces | N chambres", [-2] Ville, [-1] "NNN €"
    (la structure varie : on parse par motifs, pas par index figé.)

Type de bien : déduit de la ligne catégorie "Vente, {Type}".
  On ne garde que maisons / propriétés / longères / manoirs / fermes…
  (exclut Appartement, Bureaux, Local Commercial/Professionnel, Terrain,
   Cession De Droit Au Bail, Fonds De Commerce).

Localisation : PAS de code postal sur la carte ni de CP fiable sur la fiche
  (les pages détail ne contiennent que des CP de gabarit CSS type 23333/49800).
  → On GÉOCODE le nom de ville via l'API BAN (api-adresse.data.gouv.fr, gratuite,
    sans clé) pour obtenir code_postal + département FIABLES, puis POST-FILTRE
    par département (0 fuite garantie côté géoloc officielle).

Couverture : Mayenne (53), Maine-et-Loire / Anjou (49), Sarthe (72), + quelques
  communes isolées hors-zone (Mauléon 79, Mortagne-sur-Sèvre 85, St-Léger 86).
  Sur les départements cibles, inventaire réel mais modeste (72/49/53).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://mdi-anjoumaine.fr"
LISTING_URL = f"{BASE_URL}/bien/"
BAN_URL = "https://api-adresse.data.gouv.fr/search/"
MAX_PAGES = 12          # plafond de sécurité (~9 pages réelles)
PHOTOS_PER_CARD = 4
GEOCODE_CONCURRENCY = 6


# Types (ligne "Vente, {Type}") explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|bureau|local\s+(commercial|professionnel)|terrain|"
    r"cession|droit\s+au\s+bail|fonds\s+de\s+commerce|parking|garage|immeuble",
    re.IGNORECASE,
)
# Types conservés → libellé normalisé
_TYPE_MAP = [
    (re.compile(r"château|chateau|manoir", re.IGNORECASE), "manoir"),
    (re.compile(r"longère|longere", re.IGNORECASE), "longère"),
    (re.compile(r"moulin", re.IGNORECASE), "moulin"),
    (re.compile(r"domaine", re.IGNORECASE), "domaine"),
    (re.compile(r"ferme|corps de ferme", re.IGNORECASE), "ferme"),
    (re.compile(r"propriété|propriete", re.IGNORECASE), "propriété"),
    (re.compile(r"maison de village", re.IGNORECASE), "maison de village"),
    (re.compile(r"maison", re.IGNORECASE), "maison"),
]

# "88.56 m² | 5 pièces | 3 chambres"
_RX_CARAC = re.compile(
    r"([\d\s\xa0.,]+)\s*m²"
    r"(?:\s*\|\s*(\d+)\s*pi[eè]ce)?"
    r"(?:\s*\|\s*(\d+)\s*chambre)?",
    re.IGNORECASE,
)
_RX_PRICE = re.compile(r"^\s*[\d\s\xa0.,]+\s*€\s*$")
_RX_5DIGIT = re.compile(r"\b\d{5}\b")


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1) Récupère toutes les cartes "Vente" (filtre offre serveur)
        cards = await _fetch_all_cards(client)

        # 2) Parse + dédup par URL
        parsed: dict[str, dict] = {}
        for card in cards:
            bien = _parse_card(card)
            if not bien:
                continue
            parsed.setdefault(bien["url"], bien)
        biens = list(parsed.values())

        # 3) Géocode le nom de ville → code_postal + département (BAN, sans clé)
        sem = asyncio.Semaphore(GEOCODE_CONCURRENCY)

        async def geocode(b: dict):
            async with sem:
                cp, dept = await _geocode_ville(client, b["ville"])
                if cp:
                    b["code_postal"] = cp
                    b["departement"] = dept

        await asyncio.gather(*(geocode(b) for b in biens if b.get("ville")))

    # 4) POST-FILTRE département + prix/surface (0 fuite)
    results: list[dict] = []
    for b in biens:
        dept = b.get("departement") or ""
        if departements and dept not in departements:
            continue

        p = b.get("prix") or 0
        s = b.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        results.append(b)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[MDIAnjouMaine] Dept {dept}: {n} annonces")

    return results


async def _fetch_all_cards(client: httpx.AsyncClient) -> list:
    cards = []
    seen_urls: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = f"{LISTING_URL}?ads_filters_type_offre=Vente"
        else:
            url = f"{LISTING_URL}page/{page}/?ads_filters_type_offre=Vente"
        try:
            r = await client.get(url)
            if r.status_code != 200:
                break
        except Exception as e:
            print(f"[MDIAnjouMaine] Erreur page {page}: {e}")
            break

        page_cards = BeautifulSoup(r.text, "html.parser").select("div.bien")
        if not page_cards:
            break

        new = 0
        for c in page_cards:
            a = c.select_one("a.ann[href]") or c.select_one("a[href]")
            href = a["href"].strip() if a and a.get("href") else ""
            if href and href in seen_urls:
                continue
            if href:
                seen_urls.add(href)
            cards.append(c)
            new += 1

        if new == 0:
            break
        await asyncio.sleep(0.4)

    return cards


def _parse_card(card) -> dict | None:
    a = card.select_one("a.ann[href]") or card.select_one("a[href]")
    if not a or not a.get("href"):
        return None
    url = a["href"].strip()
    if url.startswith("/"):
        url = BASE_URL + url

    # id annonce depuis class "post-NNNNNN"
    id_annonce = None
    classes = card.get("class", [])
    for cl in classes:
        m = re.match(r"post-(\d+)", cl)
        if m:
            id_annonce = m.group(1)
            break
    if not id_annonce:
        id_annonce = url

    # Toutes les lignes texte (titres Elementor)
    lines = [
        h.get_text(" ", strip=True)
        for h in card.select(".elementor-heading-title")
    ]
    lines = [l for l in lines if l]
    if not lines:
        return None

    titre = lines[0]

    # Ligne catégorie "Vente, {Type}"
    cat_line = ""
    for l in lines:
        if l.lower().startswith("vente,") or l.lower().startswith("location,"):
            cat_line = l
            break
    type_raw = cat_line.split(",", 1)[1].strip() if "," in cat_line else ""

    # Filtre type : on ne garde que maisons/propriétés/…
    if _EXCLUDE_TYPE.search(type_raw) or _EXCLUDE_TYPE.search(titre):
        return None
    type_bien = ""
    for rx, label in _TYPE_MAP:
        if rx.search(type_raw) or rx.search(titre):
            type_bien = label
            break
    if not type_bien:
        # type inconnu/ambigu et pas de catégorie maison → on exclut par prudence
        return None

    # Caractéristiques "NN m² | N pièces | N chambres"
    surface = pieces = chambres = None
    carac_line = ""
    for l in lines:
        if "m²" in l and ("pièce" in l.lower() or "piece" in l.lower() or "|" in l):
            carac_line = l
            break
    if not carac_line:
        for l in lines:
            if "m²" in l:
                carac_line = l
                break
    if carac_line:
        m = _RX_CARAC.search(carac_line)
        if m:
            surface = _parse_num(m.group(1))
            pieces = int(m.group(2)) if m.group(2) else None
            chambres = int(m.group(3)) if m.group(3) else None

    # Prix : ligne "NNN €" pure
    prix = None
    for l in lines:
        if _RX_PRICE.match(l):
            prix = _parse_num(l)
            break

    # Ville : ligne sans m², sans €, sans virgule catégorie, non titre/badge.
    # C'est en général l'avant-dernière ligne. On retient la première candidate
    # qui n'est ni titre, ni catégorie, ni carac, ni prix, ni un badge connu.
    badges = {"nouveauté", "nouveaute", "exclusivité", "exclusivite",
              "coup de coeur", "en savoir plus", "vendu", "encore vendu"}
    ville = ""
    for l in reversed(lines):
        ll = l.lower()
        if l == titre or l == cat_line or l == carac_line:
            continue
        if "€" in l or "m²" in l:
            continue
        if ll in badges:
            continue
        if _RX_5DIGIT.search(l):
            continue
        ville = l
        break

    # Photo de couverture (background ou img lazy)
    photos = []
    img = card.select_one("img[src], img[data-src], img[data-lazy-src]")
    if img:
        src = (
            img.get("data-lazy-src")
            or img.get("data-src")
            or img.get("src")
            or ""
        )
        if src.startswith("http") and not src.startswith("data:"):
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "mdi_anjoumaine",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": None,
        "departement": "",       # rempli par géocodage ville
        "ville": ville[:80],
        "code_postal": "",        # rempli par géocodage ville
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "dpe": None,
        "photos": photos,
        "agence": "MDI Anjou-Maine",
    }


# ── Géocodage BAN ────────────────────────────────────────────────────────────

_GEO_CACHE: dict[str, tuple[str, str]] = {}


async def _geocode_ville(
    client: httpx.AsyncClient, ville: str
) -> tuple[str | None, str | None]:
    """Nom de commune → (code_postal, département) via l'API BAN officielle."""
    key = ville.strip().lower()
    if not key:
        return None, None
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]

    try:
        r = await client.get(
            BAN_URL,
            params={"q": ville, "type": "municipality", "limit": 1},
            timeout=15,
        )
        if r.status_code != 200:
            return None, None
        feats = r.json().get("features") or []
        if not feats:
            return None, None
        props = feats[0]["properties"]
        cp = props.get("postcode") or ""
        # département : préfixe citycode (gère la Corse 2A/2B) ou contexte
        citycode = props.get("citycode") or ""
        dept = citycode[:2] if citycode else (cp[:2] if cp else "")
        result = (cp or None, dept or None)
        _GEO_CACHE[key] = result
        return result
    except Exception:
        return None, None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_num(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d,\.]", "", text.replace("\xa0", " ").replace(" ", ""))
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
        search({
            "departements": criteres.departements,
            "prix_max": criteres.prix_max,
            "prix_min": getattr(criteres, "prix_min", 0),
            "surface_min": criteres.surface_min,
        })
    )
    print(f"\nTotal MDI Anjou-Maine (depts cibles): {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:15]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€ — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p — {b['type_bien']} — {b['ville']}"
        )

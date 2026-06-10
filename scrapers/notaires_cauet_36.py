"""scrapers/notaires_cauet_36.py — SELARL CAUET / MORIN-GOETGHELUCK / CHARPENTIER (notaires Indre)

Méthode : scrape_simple (httpx) — SSR HTML (front Genapi / sites-notaires.immonot.com,
          servi sous le domaine personnalisé notaires-cauet.fr).
Office mono-département implanté à Saint-Gaultier (36) → quasi-totalité des biens
en Indre (36), département cible. Couvre maisons, appartements, terrains, immeubles…

URL pattern (pagination) :
    /annonces-immobilieres.html              (page 1)
    /fr_FR/3/{N}/annonces-immobilieres.html  (page N, N≥1)
    → la première page hors-stock renvoie 0 carte (condition d'arrêt).

Cartes : div.bloc-annonce-carre  (≈ 9 / page)
  - URL détail : a[href*="annonces/detail"]
  - SEO slug (commentaire HTML / href dossiers) : "{type}-{dept}-{commune}.html"
        → le segment {dept} (ex: "indre") donne le département de façon FIABLE.
  - Type  : 1ʳᵉ ligne texte "Vente Maison" / "Vente Terrain à bâtir"…
  - Prix  : 2ᵉ colonne de la même ligne → "121 900  €"
  - Ville : ligne suivante, col gauche (.light-color) → "Rivarennes"
  - Surface : dernière ligne, col gauche → "117.0 m²" (habitable) ou "600 m2" (terrain)
  - Réf.  : dernière ligne, col droite → "Réf. 037/2723"  (préfixe 037 = Indre)
  - Photo : img[src^="/photoProduit/"]

Filtre département : le code postal n'apparaît PAS dans la liste (et le CP de la
page détail est celui de l'ÉTUDE, pas du bien) → on déduit le département du
slug SEO ({type}-{dept}-{commune}) via DEPT_NAME_TO_CODE, puis POST-FILTRE STRICT
sur les départements cibles. 0 fuite garanti (on rejette toute carte dont le slug
ne mappe pas un département cible).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import unicodedata

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.notaires-cauet.fr"
LISTING_URL = BASE_URL + "/fr_FR/3/{page}/annonces-immobilieres.html"
MAX_PAGES = 12
PHOTOS_PER_CARD = 6

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Nom de département (slug SEO, sans accent) → code INSEE.
# Couvre les départements cibles + voisins immédiats de l'Indre que l'étude
# pourrait référencer ; tout ce qui n'est pas une cible est rejeté ensuite.
DEPT_NAME_TO_CODE: dict[str, str] = {
    "sarthe": "72",
    "eure-et-loir": "28",
    "loiret": "45",
    "yonne": "89",
    "maine-et-loire": "49",
    "indre-et-loire": "37",
    "indre": "36",
    "cher": "18",
    "nievre": "58",
    "loir-et-cher": "41",
    "mayenne": "53",
}

# Types de bien à conserver (maisons / propriétés / appartements…).
_KEEP_TYPE = re.compile(
    r"maison|appartement|propriete|villa|ferme|longere|manoir|chateau|"
    r"moulin|demeure|domaine|mas|gite|corps-de-ferme|maison-de-village|"
    r"maison-de-ville|maison-de-village",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"agricole|loisirs|bois|etang|murs|viager",
    re.IGNORECASE,
)


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def _parse_slug(href: str) -> tuple[str | None, str | None]:
    """De l'URL SEO 'vente-maison-indre-rivarennes.html' → (type_slug, dept_code).

    On reconnaît le département par le nom le plus long présent dans le slug
    (eure-et-loir avant indre, etc.), puis on renvoie le type (préfixe avant le
    nom de département)."""
    slug = href.lower()
    m = re.search(r"/(?:vente|location)-([a-z0-9-]+?)\.html", slug)
    core = m.group(1) if m else slug
    core = _strip_accents(core)

    # Cherche le nom de département présent (le plus long d'abord pour éviter
    # qu'« indre » matche dans « indre-et-loire »).
    for name in sorted(DEPT_NAME_TO_CODE, key=len, reverse=True):
        marker = f"-{name}-"
        if marker in f"-{core}-":
            code = DEPT_NAME_TO_CODE[name]
            type_slug = core.split(marker.strip("-"))[0].strip("-")
            return type_slug, code
    return None, None


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
            url = LISTING_URL.format(page=page)
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[CauetNotaires] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                "div.bloc-annonce-carre"
            )
            if not cards:
                break  # première page vide → fin

            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # POST-FILTRE DÉPARTEMENT STRICT (0 fuite)
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

            if new_on_page == 0 and page > 1:
                # plus rien de nouveau (et pas la 1ʳᵉ page filtrée) → on continue
                # tout de même tant qu'il y a des cartes, mais on borne par MAX_PAGES
                pass
            await asyncio.sleep(0.5)

    print(f"[CauetNotaires] {len(results)} annonces retenues")
    return results


def _parse_card(card) -> dict | None:
    link = card.find("a", href=True)
    if not link:
        return None
    href = link["href"]
    url = href if href.startswith("http") else BASE_URL + href

    # Type + département via le slug SEO (commentaire / href dossiers présent dans le bloc)
    html = str(card)
    m_seo = re.search(r'/(?:vente|location)-[a-z0-9-]+?\.html', html)
    seo = m_seo.group(0) if m_seo else ""
    type_slug, dept = _parse_slug(seo)
    if not dept:
        return None  # slug non mappé → pas un département cible connu → on rejette

    if type_slug:
        if _EXCLUDE_TYPE.search(type_slug) and not _KEEP_TYPE.search(type_slug):
            return None
        if not _KEEP_TYPE.search(type_slug):
            return None

    # id_annonce : token oidAnnonce de l'URL détail
    m_id = re.search(r"oidAnnonce/([^/]+)", url)
    id_annonce = m_id.group(1) if m_id else url

    rows = card.select("div.row")
    type_txt = ""
    prix = None
    ville = ""
    surface = None
    ref = ""

    for row in rows:
        cols = row.select("div[class*=col]")
        texts = [c.get_text(" ", strip=True) for c in cols]
        joined = " ".join(texts)
        # Ligne type + prix
        if not type_txt and re.search(r"\b(Vente|Location)\b", joined):
            type_txt = texts[0] if texts else ""
            for t in texts:
                if "€" in t:
                    prix = _parse_price(t)
        # Ligne ville (col gauche light-color, sans €/m²/Réf.)
        if not ville:
            for c in cols:
                cls = " ".join(c.get("class", []))
                txt = c.get_text(" ", strip=True)
                if (
                    "light-color" in cls
                    and txt
                    and "€" not in txt
                    and "Honoraires" not in txt
                    and "Réf" not in txt
                    and not re.search(r"m²|m2", txt)
                ):
                    ville = txt
                    break
        # Ligne surface + réf
        for t in texts:
            if surface is None and re.search(r"\d[\d.,]*\s*m[²2]\b", t):
                surface = _parse_surface(t)
            if not ref:
                m_ref = re.search(r"R[ée]f\.?\s*([\w/\-]+)", t)
                if m_ref:
                    ref = m_ref.group(1)

    # Ville de secours depuis le slug
    if not ville and seo:
        tail = re.sub(r"\.html$", "", seo).split("-")
        ville = tail[-1].replace("_", " ").title() if tail else ""

    type_bien = _clean_type(type_txt) or (
        type_slug.replace("-", " ") if type_slug else "maison"
    )

    titre = f"{type_bien.title()} {ville}".strip()
    if ref:
        id_annonce = f"{id_annonce}|{ref}" if id_annonce else ref

    # Photo
    photos = []
    for img in card.select("img[src]"):
        src = img.get("src", "")
        if src.startswith("/photoProduit/"):
            photos.append(BASE_URL + src)
        elif src.startswith("http") and "photoProduit" in src:
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    # Pour les terrains/biens sans habitable réel, surface peut être la parcelle ;
    # on ne distingue pas finement ici (le post-filtre surface_min reste prudent).
    return {
        "source": "notaires_cauet_36",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",  # absent de la liste ; CP détail = celui de l'étude (non fiable)
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "SELARL CAUET - MORIN-GOETGHELUCK - CHARPENTIER (notaires Indre)",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clean_type(text: str) -> str:
    t = re.sub(r"^\s*(Vente|Location)\s+", "", text, flags=re.IGNORECASE).strip()
    return t.lower()


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    m = re.search(r"(\d[\d\s\xa0]*[.,]?\d*)\s*m[²2]\b", text)
    if not m:
        return None
    val = m.group(1).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        f = float(val)
        if 1 <= f <= 100000:
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
    print(f"\nTotal Cauet Notaires: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
            f" — {len(b['photos'])} photo(s)"
        )

"""scrapers/groupementimmo.py — Groupement Immobilier (réseau national de mandataires)

Méthode : scrape_simple (httpx) — SSR HTML, infra La Boite Immo / Hektor (staticlbi.com).

Filtre département CÔTÉ SERVEUR via une recherche POST :
  POST https://www.groupementimmo.fr/recherche/
       data[Search][offredem]=0
       data[Search][dep][]={NN}              ← code département sur 2 caractères
  → 302 (les critères sont stockés en session PHPSESSID) → page de résultats /recherche/.
  Le listing brut /a-vendre/{page} est NATIONAL (~37 pages) et n'est pas utilisé ici
  car il n'a pas de filtre dept dans l'URL.

IMPORTANT : ouvrir un client httpx NEUF par département (la recherche est liée à la
session ; réutiliser une session déjà « polluée » donne 0 résultat de façon erratique).

Cartes : article.row.bien  (rendues directement dans la page de résultats)
  - surface/pièces/chambres : div.surface / div.surface2 (texte numérique + icône)
  - titre   : header h1
  - loc     : span.ville-bien  →  "Ville - (CODEPOSTAL)"  (+ 1er span = type de bien)
  - desc    : p.hide-for-medium-down  (texte d'accroche)
  - prix    : div.prixx  →  "2 496 000 €"
  - réf     : div.dossier  →  "Référence : 35823"
  - url     : a[href$=.html] (slug /{id}-{slug}.html) ; id_annonce = input[value] btnSelect
  - photos  : img sur staticlbi.com (vignette + galerie commentée)

Volume : réseau national à faible inventaire. Sur les 11 départements cibles, seuls
quelques-uns ont du stock (37, 58 au dernier test ; 18 listé au dropdown mais 0 ce jour).
Tous les autres depts cibles (72/28/45/89/49/36/41/53) : 0 annonce.

Filtre dept vérifié : 0 fuite (tous les CP ramenés commencent par le dept demandé ;
sécurité supplémentaire code_postal[:2] == dept).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.groupementimmo.fr"
SEARCH_URL = f"{BASE_URL}/recherche/"
MAX_PAGES = 10           # plafond ; l'inventaire par dept tient en général sur 1 page
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien (1er span.ville-bien) à conserver : maisons / propriétés / manoirs...
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|long[èe]re|manoir|ch[âa]teau|moulin|"
    r"demeure|domaine|mas|g[îi]te|corps de ferme|maison de village|grange|"
    r"hôtel particulier|b[âa]tisse|chartreuse|pavillon",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|duplex|studio|rez de jardin|box",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    results: list[dict] = []

    for dept in departements:
        try:
            biens = await _scrape_dept(dept, prix_max, prix_min, surface_min)
            results.extend(biens)
            print(f"[GroupementImmo] Dept {dept}: {len(biens)} annonces")
        except Exception as e:
            print(f"[GroupementImmo] Erreur dept {dept}: {e}")
        await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    dept: str, prix_max: int, prix_min: int, surface_min: int
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    # Client NEUF par dept : la recherche est liée à la session PHPSESSID.
    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # POST de recherche → 302 → page de résultats (critères stockés en session)
        data = {"data[Search][offredem]": "0", "data[Search][dep][]": dept}
        r = await client.post(
            SEARCH_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code != 200:
            return biens

        _consume_page(r.text, dept, biens, seen_ids, prix_max, prix_min, surface_min)

        # Pagination éventuelle (rare) : /recherche/2, /recherche/3 ... dans la session
        for page in range(2, MAX_PAGES + 1):
            await asyncio.sleep(0.4)
            rp = await client.get(f"{SEARCH_URL}{page}")
            if rp.status_code != 200:
                break
            before = len(biens)
            cards = BeautifulSoup(rp.text, "html.parser").select("article.bien")
            if not cards:
                break
            _consume_page(
                rp.text, dept, biens, seen_ids, prix_max, prix_min, surface_min
            )
            if len(biens) == before:
                break

    return biens


def _consume_page(
    html: str,
    dept: str,
    biens: list[dict],
    seen_ids: set[str],
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> None:
    soup = BeautifulSoup(html, "html.parser")
    for card in soup.select("article.bien"):
        try:
            bien = _parse_card(card, dept)
        except Exception:
            continue
        if not bien:
            continue

        # FILTRE DÉPARTEMENT strict (sécurité anti-fuite)
        cp = bien.get("code_postal") or ""
        if not cp or cp[:2] != dept:
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


def _parse_card(card, dept: str) -> dict | None:
    # URL de la fiche : a[href] terminant par .html
    href = ""
    for a in card.select("a[href]"):
        h = a.get("href", "")
        if h.endswith(".html"):
            href = h
            break
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce : input caché du bouton sélection (sinon id numérique du slug)
    id_annonce = ""
    inp = card.select_one(".btnSelect input[value]")
    if inp and inp.get("value"):
        id_annonce = inp["value"].strip()
    if not id_annonce:
        m = re.search(r"/(\d+)-", href)
        if m:
            id_annonce = m.group(1)
    if not id_annonce:
        id_annonce = url

    # Localisation : spans.ville-bien → [0]=type, [1]="Ville - (CODEPOSTAL)"
    villes = [s.get_text(" ", strip=True) for s in card.select("span.ville-bien")]
    type_txt = ""
    loc_txt = ""
    for v in villes:
        if re.search(r"\(\d{5}\)", v):
            loc_txt = v
        elif v.strip().strip("|").strip():
            type_txt = type_txt or v.strip().strip("|").strip()
    ville, code_postal = _parse_loc(loc_txt)

    # Type de bien + filtre maisons/propriétés
    type_bien = (type_txt or "").strip() or "maison"
    if _EXCLUDE_TYPE.search(type_bien) and not _KEEP_TYPE.search(type_bien):
        return None
    if not _KEEP_TYPE.search(type_bien):
        # type ambigu → on exclut par prudence (réseau publie surtout des maisons)
        return None

    # Titre
    h1 = card.select_one("header h1") or card.select_one("h1")
    titre = h1.get_text(" ", strip=True) if h1 else ""
    titre = re.sub(r"\s+", " ", titre).strip()
    if not titre:
        titre = f"{type_bien} {ville}".strip()

    # Description
    desc_el = card.select_one("p.hide-for-medium-down") or card.select_one(
        "p.show-for-medium-only"
    )
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Prix
    prix_el = card.select_one(".prixx")
    prix = _parse_num(prix_el.get_text(" ", strip=True)) if prix_el else None

    # Surface / pièces / chambres : blocs .surface (texte) avec icônes dédiées
    surface = pieces = chambres = None
    for blk in card.select("div.surface, div.surface2"):
        num = _leading_num(blk.get_text(" ", strip=True))
        if num is None:
            continue
        if blk.select_one(".icon-detail_surface"):
            surface = surface or num
        elif blk.select_one(".icon-detail_pieces"):
            pieces = pieces or int(num)
        elif blk.select_one(".icon-detail_chambres"):
            chambres = chambres or int(num)

    # Photos (staticlbi.com) : vignette + galerie commentée dans le HTML
    photos: list[str] = []
    for img in card.select("img[src*='staticlbi.com']"):
        src = img.get("src") or ""
        if "/images/biens/" not in src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        if src not in photos:
            photos.append(src)
    # liens galerie originale (commentés mais présents dans le markup)
    for a in card.select("a[href*='staticlbi.com']"):
        src = a.get("href") or ""
        if "/images/biens/" in src:
            if src.startswith("//"):
                src = "https:" + src
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "groupementimmo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien.lower()[:40],
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Groupement Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Vou - (37240)' → ('Vou', '37240')"""
    cp = ""
    m = re.search(r"\((\d{5})\)", text)
    if m:
        cp = m.group(1)
    ville = re.sub(r"\s*-?\s*\(\d{5}\)\s*$", "", text).strip(" -|")
    return ville, cp


def _parse_num(text: str) -> float | None:
    """'2 496 000 €' / '670 m²' → float"""
    cleaned = re.sub(r"[^\d,\.]", "", text.replace("\xa0", " ").replace(" ", ""))
    cleaned = cleaned.replace(",", ".")
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _leading_num(text: str) -> float | None:
    """Premier nombre en tête de bloc (la valeur précède l'icône/label)."""
    m = re.match(r"\s*([\d\s\xa0]+)", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        return float(val) if val else None
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
    print(f"\nTotal Groupement Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    leaks = [b for b in biens if b["code_postal"][:2] != b["departement"]]
    print(f"FUITES hors-dept : {len(leaks)}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

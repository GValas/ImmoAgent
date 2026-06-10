"""scrapers/reseau_expertimo.py — Réseau Expertimo (réseau de mandataires, ~1000 conseillers)

Méthode : scrape_simple (httpx) — SSR HTML, pas de Playwright, pas de Cloudflare.

⚠ Domaine distinct de expertimo.com (en blacklist : connexion refusée). Ici
   www.reseau-expertimo.fr est bien accessible (200) et sert ses résultats en SSR.

Particularité importante (corrige l'hypothèse initiale) :
  - La PAGE /a-vendre/1 n'est PAS un listing mais le formulaire de recherche
    (homepage). Les résultats vivent sous /nos-biens/{token}/{page}.
  - L'URL DÉTAIL est /vente/{idville-ville}/{type}/t{N}/{ref-slug}/  → le 1er
    segment est un ID INTERNE de ville (ex 7840-sermaises), PAS un code
    département. Donc PAS de filtre dept dans l'URL détail.

Filtre département CÔTÉ SERVEUR (vérifié, 0 fuite) :
  1. L'autocomplete GET /i/javascript/localisationAllItems?term={nom_dept}
     renvoie un item "dep-{id}" → "Loiret - Dep 45". On résout l'id interne du
     département cible (DEPT_QUERY ci-dessous).
  2. POST {localisation[loc][]: dep-{id}, offredem[]: 1} sur
     /nos-biens/{token}/1 → redirige vers un token filtré ; on pagine ce token.
     Le serveur ne renvoie QUE le département demandé (testé : dept 45 → 58 biens,
     100% en 45xxx, sur 7 pages de 9 cartes).

Cartes : article.property-listing-v1__item
  - URL   : a.item__title[href]  → segment type d'URL (maison/propriete/...)
  - Loc   : .title__content-1  →  "Ville (CODEPOSTAL)"
  - Titre : .title__content-2
  - Prix  : .item__price .__price-value  →  "225 000 €"
  - Opts  : .item__options  →  "6 Pièce(s) 4 Chambre(s) 1 Salle(s) de bain"
  - Réf   : .item__reference  →  "Réf : 91373"
  - Photo : .item__img[src] (1 vignette en liste)

Surface habitable / terrain : absents de la carte liste → tentative depuis le
  slug du titre (ex "...maison-de-maitre-150m2..."), sinon None.

Post-filtre STRICT code_postal[:2] == dept en plus du filtre serveur → 0 fuite.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.reseau-expertimo.fr"
HOME_URL = f"{BASE_URL}/a-vendre/1"
SEARCH_SEED = f"{BASE_URL}/nos-biens/xdpezdofyyytkyf3/1"  # token "all" de départ
LOCAL_ITEMS = f"{BASE_URL}/i/javascript/localisationAllItems"
MAX_PAGES = 15
PHOTOS_PER_CARD = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Terme de recherche autocomplete par département cible → résout l'item "dep-{id}".
DEPT_QUERY: dict[str, str] = {
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
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|maison-de-village",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|viager",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
    ) as client:
        # pose les cookies de session (host parfois lent → quelques retries)
        ok = False
        for attempt in range(4):
            try:
                await client.get(HOME_URL)
                ok = True
                break
            except Exception as e:
                if attempt == 3:
                    print(f"[ReseauExpertimo] Erreur init session : {e}")
                await asyncio.sleep(2)
        if not ok:
            return results

        for dept in departements:
            query = DEPT_QUERY.get(dept)
            if not query:
                continue
            try:
                dep_id = await _resolve_dep_id(client, dept, query)
                if not dep_id:
                    print(f"[ReseauExpertimo] Dept {dept}: dep-id introuvable")
                    continue
                biens = await _scrape_dept(
                    client, dept, dep_id, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[ReseauExpertimo] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[ReseauExpertimo] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _resolve_dep_id(
    client: httpx.AsyncClient, dept: str, query: str
) -> str | None:
    """Résout l'item 'dep-{id}' du département via l'autocomplete localisation."""
    headers = {"X-Requested-With": "XMLHttpRequest"}
    for term in (query, query.replace("-", " "), dept):
        try:
            r = await client.get(LOCAL_ITEMS, params={"term": term}, headers=headers)
        except Exception:
            continue
        if r.status_code != 200 or not r.text.strip():
            continue
        try:
            data = json.loads(r.text)
        except (json.JSONDecodeError, ValueError):
            continue
        for key, label in data.items():
            m = re.match(r"dep-(\d+)", key)
            if m and re.search(rf"Dep\s+{dept}\b", str(label)):
                return key
    return None


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    dep_id: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    # POST de la recherche filtrée → récupère le token filtré
    data = {"localisation[loc][]": dep_id, "offredem[]": "1"}
    r = await client.post(SEARCH_SEED, data=data)
    if r.status_code != 200:
        return []
    token = str(r.url).rstrip("/").rsplit("/", 2)[-2]
    list_base = f"{BASE_URL}/nos-biens/{token}"

    biens: list[dict] = []
    seen_ids: set[str] = set()

    # la 1re réponse (r) correspond déjà à la page 1
    first = r.text
    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            html = first
        else:
            rr = await client.get(f"{list_base}/{page}")
            if rr.status_code != 200:
                break
            html = rr.text

        cards = BeautifulSoup(html, "html.parser").select(
            "article.property-listing-v1__item"
        )
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

            # Sécurité 0-fuite : on n'accepte que le département cible
            if bien["code_postal"] and bien["code_postal"][:2] != dept:
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
    link = card.select_one("a.item__title")
    href = link.get("href", "") if link else ""
    if not href or "/vente/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL : /vente/{idville-ville}/{type}/t{N}/{ref-slug}/
    parts = [p for p in href.split("/") if p]
    # parts ≈ ['vente', '7840-sermaises', 'maison', 't6', '226556-...']
    type_seg = parts[2] if len(parts) > 2 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean) and not _KEEP_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        return None
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # Localisation : "Ville (CODEPOSTAL)"
    loc_el = card.select_one(".title__content-1")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)

    # Titre
    title_el = card.select_one(".title__content-2")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Référence (id_annonce)
    ref_el = card.select_one(".item__reference")
    ref = ""
    if ref_el:
        m = re.search(r"R[ée]f\s*:?\s*(\S+)", ref_el.get_text(" ", strip=True))
        if m:
            ref = m.group(1)
    # id numérique du slug final en secours
    id_num = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_num = m.group(1)
    id_annonce = id_num or ref or url

    # Prix
    price_el = card.select_one(".item__price .__price-value") or card.select_one(
        ".item__price"
    )
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Options : pièces / chambres
    opts_el = card.select_one(".item__options")
    opts_text = opts_el.get_text(" ", strip=True) if opts_el else ""
    pieces = _parse_int(r"(\d+)\s*Pi[eè]ce", opts_text)
    chambres = _parse_int(r"(\d+)\s*Chambre", opts_text)

    # Pièces en secours : segment tN de l'URL
    if pieces is None and len(parts) > 3:
        m = re.match(r"^t(\d+)$", parts[3])
        if m:
            pieces = int(m.group(1))

    # Surface : pas sur la carte → tentative depuis le slug du titre / titre
    surface = _parse_surface_hab(parts[-1]) or _parse_surface_hab(titre)

    # Photo (vignette unique en liste)
    photos = []
    img = card.select_one(".item__img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "reseau_expertimo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
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
        "agence": "Réseau Expertimo",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Céret (66400)' → ('Céret', '66400')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_surface_hab(text: str) -> float | None:
    """Cherche 'NNN m2' / 'NNNm2' / 'NNN m²' dans le texte/slug."""
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0]*)\s*m(?:2|²)", text, re.IGNORECASE)
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
    print(f"\nTotal Réseau Expertimo: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

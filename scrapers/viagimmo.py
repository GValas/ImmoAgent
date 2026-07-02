"""scrapers/viagimmo.py — Viagimmo (réseau national de viager)

Méthode : api_inoff (httpx) — AJAX admin-ajax WordPress renvoyant du HTML SSR.

Particularité : la grille d'annonces de /nos-annonces-immobilieres/ est chargée
en AJAX. La page d'accueil pose une variable JS `ajax_var.nonce` ; la grille est
ensuite obtenue par :
    POST https://viagimmo.fr/wp-admin/admin-ajax.php?{querystring}&action=getAnnonces
    corps : nonce={nonce}
où {querystring} reprend les filtres de recherche (ville, distance, page...).
La réponse est du HTML : des cartes `div.annonce-slider-item`.

Filtre département : le moteur n'expose PAS de filtre « département » direct, mais
un filtre `ville={VILLE-NN}&distance={km}` (rayon autour d'une ville). On interroge
donc la PRÉFECTURE de chaque département cible avec un rayon généreux (45 km), ce
qui RAMÈNE des biens des départements limitrophes → la carte ne contient pas de
code postal, on récupère donc le CP sur la PAGE DÉTAIL de chaque annonce, puis on
applique un POST-FILTRE STRICT `code_postal[:2] == dept` (0 fuite vérifiée).

Cartes (div.annonce-slider-item) :
  - data-annonce-id           → id_annonce
  - attribut href             → /nos-annonces/{id}/  (URL détail)
  - .type                     → "Viager occupé" / "Viager libre" / "Vente à terme"...
  - .row.title / a.titleLink  → "Maison 4 pièces - 88m²" (type + pièces + surface)
  - .left.lieu                → ville (sans CP)
  - .mandat                   → "Mandat : 39VO17"
  - span.bouquet / .content span.price → "Bouquet : 67 440 €" (prix affiché = bouquet/comptant)
  - img.swiper-lazy[data-src] → photos

Page détail (/nos-annonces/{id}/) :
  - "VILLE (CODEPOSTAL)"       → code_postal (filtre dept STRICT)
  - "DPE : X"                  → dpe
  - <h1> "Maison Cholet 4 pièce(s) 88 m2" → secours surface/pièces

NB viager : le prix affiché est le BOUQUET (ou prix comptant), pas la valeur vénale ;
il est nettement inférieur au marché. On le renseigne dans `prix` (seul montant
exposé en liste) ; le filtre prix_min du pipeline est donc volontairement laissé
faire son office (la plupart des viagers seront écartés — comportement attendu).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import urllib.parse

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://viagimmo.fr"
AJAX_URL = "https://viagimmo.fr/wp-admin/admin-ajax.php"
LISTE_URL = "https://viagimmo.fr/nos-annonces-immobilieres/?_page=1&page_recherche"
MAX_PAGES = 5
SEARCH_RADIUS_KM = 45
PHOTOS_PER_CARD = 10


# Préfecture de chaque département cible → valeur du paramètre `ville` ({VILLE-NN}).
# Le rayon SEARCH_RADIUS_KM couvre le département ; le post-filtre CP[:2] retire les fuites.
DEPT_PREFECTURE: dict[str, str] = {
    "72": "LE MANS-72",
    "28": "CHARTRES-28",
    "45": "ORLEANS-45",
    "89": "AUXERRE-89",
    "49": "ANGERS-49",
    "37": "TOURS-37",
    "36": "CHATEAUROUX-36",
    "18": "BOURGES-18",
    "58": "NEVERS-58",
    "41": "BLOIS-41",
    "53": "LAVAL-53",
}

# Types conservés : on ne garde que les maisons / propriétés (pas appartement/immeuble/terrain)
_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|g[iî]te|corps de ferme|maison de village",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"studio|loft|duplex",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        nonce = await _get_nonce(client)
        if not nonce:
            print("[Viagimmo] Impossible de récupérer le nonce AJAX — abandon")
            return results

        for dept in departements:
            ville = DEPT_PREFECTURE.get(dept)
            if not ville:
                continue
            try:
                biens = await _scrape_dept(
                    client, nonce, dept, ville, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[Viagimmo] Dept {dept}: {len(biens)} annonces (post-filtre CP)")
            except Exception as e:
                print(f"[Viagimmo] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _get_nonce(client: httpx.AsyncClient) -> str | None:
    try:
        r = await client.get(LISTE_URL)
        m = re.search(r'"nonce"\s*:\s*"([a-f0-9]+)"', r.text)
        return m.group(1) if m else None
    except Exception as e:
        print(f"[Viagimmo] Erreur nonce: {e}")
        return None


async def _scrape_dept(
    client: httpx.AsyncClient,
    nonce: str,
    dept: str,
    ville: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        cards = await _fetch_cards(client, nonce, ville, page)
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
            aid = bien["id_annonce"]
            if aid in seen_ids:
                continue
            seen_ids.add(aid)

            # La carte n'a pas de CP → on le récupère sur la page détail (filtre STRICT)
            cp, dpe = await _fetch_cp_dpe(client, bien["url"])
            if not cp or cp[:2] != dept:
                continue  # fuite hors département → écarté
            bien["code_postal"] = cp
            if dpe:
                bien["dpe"] = dpe

            # Filtres structurels (sans exclure si champ manquant)
            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            biens.append(bien)
            new_on_page += 1
            await asyncio.sleep(0.3)

        if new_on_page == 0 and len(cards) < 20:
            break
        await asyncio.sleep(0.4)

    return biens


async def _fetch_cards(
    client: httpx.AsyncClient, nonce: str, ville: str, page: int
) -> list:
    qs = (
        "ville=" + urllib.parse.quote(ville)
        + f"&distance={SEARCH_RADIUS_KM}"
        + "&order=DESC&orderBy=date_mandat&layout=grid"
        + f"&_page={page}&page_recherche&action=getAnnonces"
    )
    r = await client.post(
        AJAX_URL + "?" + qs,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": LISTE_URL,
        },
        content="nonce=" + nonce,
    )
    if r.status_code != 200:
        return []
    return BeautifulSoup(r.text, "html.parser").select(".annonce-slider-item")


def _parse_card(card, dept: str) -> dict | None:
    aid = card.get("data-annonce-id") or ""
    href = card.get("href", "") or ""
    if not href:
        link = card.select_one("a.titleLink")
        href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href
    if not aid:
        m = re.search(r"/nos-annonces/(\d+)/?", url)
        aid = m.group(1) if m else url

    # Type de viager (occupé/libre/vente à terme) — informatif
    type_el = card.select_one(".type")
    type_viager = type_el.get_text(strip=True) if type_el else ""

    # Titre : "Maison 4 pièces - 88m²"
    title_el = card.select_one(".row.title") or card.select_one("a.titleLink")
    titre_brut = title_el.get_text(" ", strip=True) if title_el else ""

    # Type de bien : on exige une maison/propriété
    type_bien_seg = titre_brut.split("-")[0].split(" ")[0] if titre_brut else ""
    if _EXCLUDE_TYPE.search(titre_brut) and not _KEEP_TYPE.search(titre_brut):
        return None
    if not _KEEP_TYPE.search(titre_brut):
        return None
    type_bien = (type_bien_seg or "maison").lower()

    # Ville (sans CP — le CP vient de la page détail)
    lieu_el = card.select_one(".left.lieu")
    ville = lieu_el.get_text(strip=True) if lieu_el else ""

    # Pièces / surface depuis le titre
    pieces = _first_int(r"(\d+)\s*pi[eè]ce", titre_brut)
    surface = _first_float(r"(\d[\d\s\xa0]*)\s*m", titre_brut)

    # Prix affiché = bouquet / comptant
    price_el = card.select_one(".content span.price") or card.select_one("span.bouquet")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Mandat (référence)
    mandat_el = card.select_one(".mandat")
    mandat = ""
    if mandat_el:
        mm = re.search(r"([0-9A-Z]{3,})", mandat_el.get_text(" ", strip=True))
        mandat = mm.group(1) if mm else ""

    # Photos
    photos = []
    for img in card.select("img.swiper-lazy, img[data-src]"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = list(dict.fromkeys(photos))[:PHOTOS_PER_CARD]

    titre = titre_brut
    if ville:
        titre = f"{titre_brut} — {ville}".strip(" —")
    if type_viager:
        titre = f"{titre} ({type_viager})"

    return {
        "source": "viagimmo",
        "url": url,
        "id_annonce": mandat or str(aid),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": type_viager,
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",  # renseigné après fetch détail
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Viagimmo",
    }


async def _fetch_cp_dpe(client: httpx.AsyncClient, url: str) -> tuple[str, str | None]:
    """Récupère (code_postal, dpe) sur la page détail. Best-effort."""
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return "", None
        t = r.text
        cp = ""
        m = re.search(r"\(\s*(\d{5})\s*\)", t)
        if m:
            cp = m.group(1)
        dpe = None
        md = re.search(r"DPE\s*:?\s*([A-G])\b", t)
        if md:
            dpe = md.group(1)
        return cp, dpe
    except Exception:
        return "", None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text.replace("\xa0", ""))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _first_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _first_float(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
        return f if 8 <= f <= 2000 else None
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
    print(f"\nTotal Viagimmo: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:60]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

"""scrapers/leggett_prestige.py — Leggett Prestige (vieilles pierres / prestige rural)

Méthode : scrape_simple (httpx) — SSR HTML
Domaine : leggettprestige.com — DISTINCT de leggett.fr / leggett-immobilier.com
          (ces deux-là sont blacklistés : 403 anti-bot). leggettprestige.com
          répond 200 en SSR pur sous UA Chrome (aucun JS requis).

URL pattern : /chateau-manoir/page:{N}  (listing NATIONAL châteaux & manoirs).
  Le portail propose un filtre "towns" mais il est peuplé en AJAX (select vide
  côté serveur) et aucune URL slug région/département fiable n'existe (les
  patterns /department:NN, /maine-et-loire/… renvoient 404).
  → STRATÉGIE FILTRE DÉPARTEMENT : scrape national + POST-FILTRE STRICT sur le
    NOM de département lu dans la carte (localisation), mappé vers son code via
    DEPT_NAMES. 0 fuite garantie : un bien dont le nom de dept ne fait pas
    partie des cibles est rejeté.

Cartes : div.results-list > div.column
  - URL    : a[href*=/view/]              → /luxury-property-for-sale/view/{REF}
  - Réf    : div.icon-heart-fav[id]  (ou .ref-exclusive-new "Réf : XXX")
  - Titre  : p.propname
  - Loc    : .location .primary           → texte nu = "House in {Ville}",
             span = "{Département} - {Région ancienne} ({Région})"
  - Info   : p.info-property              → "N bed | N bath | Floor NNNm² | Plot NNNm²"
  - Prix   : p.price                      → "€1,690,000" (ou range "€X€Y" → on garde le 1er)
  - Photos : img[src] vers image.hestia.immo

Pas de code postal dans la liste (localisation au niveau commune/département) :
  code_postal = None, departement = code mappé depuis le nom.

Couverture observée (châteaux/manoirs nationaux, juin 2026) sur ~12 pages :
  37≈31, 45≈20, 41≈12, 49≈1, 72≈1 ; autres cibles faibles/0. Prestige : la
  plupart des biens dépassent prix_max=600k, le filtre prix en laisse passer peu.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import unicodedata

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://leggettprestige.com"
LIST_PATH = "/chateau-manoir/page:{page}"
MAX_PAGES = 14
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Nom de département (normalisé sans accents, tirets) → code, pour les cibles.
DEPT_NAMES: dict[str, str] = {
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


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Codes cibles présents à la fois dans les critères ET dans notre mapping de noms.
    target_codes = {c for c in DEPT_NAMES.values() if c in departements}
    if not target_codes:
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = BASE_URL + LIST_PATH.format(page=page)
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[LeggettPrestige] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                "div.results-list > div.column"
            )
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card, target_codes)
                except Exception:
                    continue
                if not bien:
                    continue

                # POST-FILTRE DÉPARTEMENT STRICT (0 fuite) : déjà mappé dans _parse_card,
                # on re-vérifie l'appartenance aux cibles.
                if bien["departement"] not in target_codes:
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

            print(
                f"[LeggettPrestige] Page {page}: {len(cards)} cartes, "
                f"{new_on_page} retenues (cibles)"
            )
            await asyncio.sleep(0.6)

    print(f"[LeggettPrestige] Total retenu : {len(results)}")
    return results


def _parse_card(card, target_codes: set[str]) -> dict | None:
    link = card.find("a", href=True)
    if not link or "/view/" not in link["href"]:
        return None
    href = link["href"]
    url = href if href.startswith("http") else BASE_URL + href

    # Référence / id_annonce
    heart = card.select_one(".icon-heart-fav")
    ref = heart.get("id", "").strip() if heart else ""
    if not ref:
        ref_el = card.select_one(".ref-exclusive-new")
        if ref_el:
            m = re.search(r"R[ée]f\s*:\s*([A-Z0-9]+)", ref_el.get_text(" ", strip=True))
            if m:
                ref = m.group(1)
    if not ref:
        ref = href.rstrip("/").split("/")[-1]
    id_annonce = ref

    # Localisation : nom de ville (texte nu) + span "Département - Région..."
    loc = card.select_one(".location .primary")
    if not loc:
        return None
    span = loc.find("span")
    dept_region = span.get_text(" ", strip=True) if span else ""
    dept_raw = dept_region.split(" - ")[0].strip() if dept_region else ""
    dept_code = DEPT_NAMES.get(_norm(dept_raw))
    if dept_code is None or dept_code not in target_codes:
        return None  # hors zone → rejet immédiat (0 fuite)

    # Ville = premier nœud texte de .primary (souvent "House in {Ville}" / "Château in {Ville}")
    ville = ""
    for node in loc.contents:
        if isinstance(node, str) and node.strip():
            ville = node.strip()
            break
    ville = re.sub(r"^(?:House|Château|Castle|Manor|Property|Villa)\s+in\s+", "", ville, flags=re.I)
    ville = ville.strip() or dept_raw

    # Titre
    title_el = card.select_one("p.propname")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"Château / manoir — {ville}"

    # Prix : "€1,690,000" ou range "€X€Y" → on garde le 1er (le plus bas affiché)
    price_el = card.select_one("p.price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Caractéristiques : "N bed | N bath | Floor NNNm² | Plot NNNm²"
    info_el = card.select_one("p.info-property")
    info_text = info_el.get_text(" ", strip=True) if info_el else ""
    chambres = _parse_int(r"(\d+)\s*bed", info_text)
    surface = _parse_m2(r"Floor\s*([\d\s,\.]+)\s*m", info_text)
    surface_terrain = _parse_m2(r"Plot\s*([\d\s,\.]+)\s*m", info_text)

    # Photos
    photos = []
    for img in card.select("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src.startswith("http") and "hestia.immo" in src and src not in photos:
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "leggett_prestige",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "château / manoir",
        "description": "",
        "departement": dept_code,
        "ville": ville[:80],
        "code_postal": None,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Leggett Prestige",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    """'Maine-et-Loire' → 'maine-et-loire' (sans accents, espaces→tirets)."""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]+", "-", t.lower()).strip("-")


def _parse_price(text: str) -> float | None:
    """'€1,690,000' ou '€532,000€560,000' → 532000.0 (1er montant rencontré)."""
    m = re.search(r"([\d][\d,\.\s]*\d)", text)
    if not m:
        return None
    cleaned = re.sub(r"[,\s\xa0]", "", m.group(1))
    cleaned = re.sub(r"\.(?=\d{3}\b)", "", cleaned)  # séparateur de milliers à point
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_m2(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    val = re.sub(r"[,\s\xa0]", "", m.group(1))
    try:
        f = float(val)
        return f if f > 0 else None
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
    print(f"\nTotal Leggett Prestige: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

"""
scrapers/proprietes_privees.py — Propriétés Privées (réseau mandataires)
Méthode : httpx pur + parsing HTML (.trade-item-container) — le SSR Nuxt rend
la liste complète sans JS, donc pas de Playwright nécessaire.
URL : /achat/maison/{slug-dept} (slug = nom dept sans numéro, ex: sarthe)
Pas de <a> dans les cards — URL construite depuis <p class="trade-reference">Ref. XXXRRN</p>

Localisation du BIEN (pas de l'agence) :
  - Sur la liste, `.trade-location` donne la ville/CP du bien (fiable, par card).
  - Repli fiche détail (concurrence limitée) quand la liste ne donne pas une
    localisation cohérente avec le département cible : on lit le tableau de
    localisation du payload Nuxt ("<slug>-<cp>","VILLE","<cp>",lat,lon) qui est
    celui du bien — à distinguer du bloc conseiller `location:{...label:"..."}`
    qui porte l'adresse du mandataire.

Interface : async def search(criteres: dict) -> list[dict]
"""
import re
import asyncio

import httpx
from bs4 import BeautifulSoup


BASE_URL = "https://www.proprietes-privees.com"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Slugs : nom seul, sans numéro (ex: "sarthe" et non "sarthe-72")
DEPT_SLUGS = {
    "72": "sarthe", "28": "eure-et-loir", "45": "loiret",
    "89": "yonne", "49": "maine-et-loire", "37": "indre-et-loire",
    "36": "indre", "18": "cher", "58": "nievre",
    "69": "rhone", "33": "gironde", "34": "herault",
    "44": "loire-atlantique", "31": "haute-garonne",
    "67": "bas-rhin", "76": "seine-maritime", "59": "nord",
    "38": "isere", "06": "alpes-maritimes", "83": "var", "13": "bouches-du-rhone",
    "75": "paris", "92": "hauts-de-seine", "93": "seine-saint-denis", "94": "val-de-marne",
    "84": "vaucluse", "26": "drome", "30": "gard", "11": "aude",
    "63": "puy-de-dome", "03": "allier", "23": "creuse",
    "41": "loir-et-cher", "61": "orne", "53": "mayenne",
    "86": "vienne", "79": "deux-sevres", "85": "vendee", "87": "haute-vienne",
}

MAX_PAGES = 5
DETAIL_CONCURRENCY = 5

# Localisation du bien dans le payload Nuxt :
#   ...,"city","<slug>-<cp>","VILLE","<cp>",<lat>,<lon>,...
# (le bloc conseiller est `location:{placeId:"...",label:"..."}` — non capté ici)
_DETAIL_LOC_RE = re.compile(
    r'"([a-z0-9][a-z0-9\-]*?)-(\d{5})",'
    r'"([A-ZÀ-Ÿ0-9 \-\']{2,60})",'
    r'"(\d{5})",-?\d+\.\d+,-?\d+\.\d+'
)


async def search(criteres: dict) -> list[dict]:
    departements = criteres.get("departements", [])
    prix_max = criteres.get("prix_max", 600000)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 80)

    results = []
    async with httpx.AsyncClient(
        headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"},
        follow_redirects=True,
        timeout=30,
    ) as client:
        for dept in departements:
            try:
                biens = await _scrape_dept(client, str(dept), prix_min, prix_max, surface_min)
                results.extend(biens)
                print(f"[PropPrivées] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[PropPrivées] Erreur dept {dept}: {e}")

    return results


async def _scrape_dept(client, dept: str, prix_min: int, prix_max: int, surface_min: int) -> list[dict]:
    slug = DEPT_SLUGS.get(dept)
    if not slug:
        return []

    biens = []
    seen_ids = set()

    for page_num in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/achat/maison/{slug}"
        if page_num > 1:
            url += f"?page={page_num}"

        try:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            print(f"[PropPrivées] Dept {dept} page {page_num}: {e}")
            break

        cards = _parse_html(html, dept)
        if not cards:
            break

        new_found = 0
        for b in cards:
            if b["id_annonce"] in seen_ids:
                continue
            if b.get("prix") and prix_max and b["prix"] > prix_max:
                continue
            if prix_min and b.get("prix") and b["prix"] < prix_min:
                continue
            if b.get("surface") and surface_min and b["surface"] < surface_min:
                continue
            seen_ids.add(b["id_annonce"])
            biens.append(b)
            new_found += 1

        if new_found == 0 and page_num > 1:
            break

    # Repli fiche détail : pour les biens dont la localisation liste est absente
    # ou incohérente avec le département cible, récupérer la vraie loc du bien.
    await _resolve_locations(client, biens, dept)

    # Sécurité dépt : ne pas laisser fuiter un bien hors département cible une
    # fois la vraie localisation connue.
    coherent = [b for b in biens if not b["code_postal"] or b["code_postal"][:2] == dept]
    leaked = len(biens) - len(coherent)
    if leaked:
        print(f"[PropPrivées] Dept {dept}: {leaked} annonces écartées (hors département)")
    for b in coherent:
        b.pop("_type_str", None)
    return coherent


async def _resolve_locations(client, biens: list[dict], dept: str) -> None:
    """Complète/corrige la localisation via la fiche détail quand nécessaire."""
    to_fix = [
        b for b in biens
        if not b["code_postal"] or b["code_postal"][:2] != dept
    ]
    if not to_fix:
        return

    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def fix(b):
        async with sem:
            loc = await _location_from_detail(client, b["url"])
        if loc:
            ville, cp = loc
            b["ville"] = ville[:80]
            b["code_postal"] = cp
            b["departement"] = cp[:2]
            # rafraîchir le titre dérivé si besoin
            b["titre"] = _make_titre(b.get("_type_str", "Maison"), b.get("pieces"), ville)

    await asyncio.gather(*(fix(b) for b in to_fix), return_exceptions=True)


async def _location_from_detail(client, url: str) -> tuple[str, str] | None:
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except Exception:
        return None
    m = _DETAIL_LOC_RE.search(resp.text)
    if not m:
        return None
    ville = m.group(3).strip()
    cp = m.group(4)
    return ville, cp


def _parse_html(html: str, dept: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".trade-item-container")
    results = []
    for card in cards:
        try:
            bien = _parse_card(card, dept)
            if bien:
                results.append(bien)
        except Exception:
            continue
    return results


def _make_titre(type_str: str, pieces, ville: str) -> str:
    return f"{type_str} {pieces or ''} p. {ville}".strip()[:150]


def _parse_card(card, dept: str) -> dict | None:
    # Référence : <p class="trade-reference">Ref. 433855RRN</p>
    ref_el = card.select_one(".trade-reference")
    ref_text = ref_el.get_text(strip=True) if ref_el else ""
    ref_m = re.search(r"[A-Z0-9]{6,}", ref_text)
    ad_id = ref_m.group(0) if ref_m else ""
    if not ad_id:
        return None

    # URL construite depuis la référence (les cards n'ont pas de <a href>)
    url = f"{BASE_URL}/annonces/{ad_id}"

    # Prix : <p class="trade-price">143 500 €</p>
    # Le site utilise des espaces insécables (U+00A0) et fines (U+202F) comme
    # séparateurs de milliers → on les supprime avant le parsing.
    price_el = card.select_one(".trade-price")
    prix = None
    if price_el:
        raw = re.sub(r"\s", "", price_el.get_text())
        prix = _re_float(r"(\d+)\s*€", raw)

    # Surface, pièces, chambres : balises .trade-feature avec img[alt] + <p> valeur
    surface = None
    pieces = None
    chambres = None
    for feat in card.select(".trade-feature"):
        img = feat.select_one("img")
        val_el = feat.select_one("p")
        if not img or not val_el:
            continue
        alt = (img.get("alt") or "").lower()
        val_text = val_el.get_text(strip=True)
        if "superficie" in alt or "surface" in alt:
            surface = _re_float(r"(\d+(?:[.,]\d+)?)", val_text)
        elif "pièces" in alt or "piece" in alt:
            pieces = _re_int(r"(\d+)", val_text)
        elif "chambre" in alt or "bedroom" in alt:
            chambres = _re_int(r"(\d+)", val_text)

    # Description + terrain + DPE
    desc_el = card.select_one(".trade-description, [class*='desc']")
    desc_text = desc_el.get_text(" ", strip=True) if desc_el else card.get_text(" ", strip=True)
    terrain_m = re.search(r"(?:terrain|parcelle)[^\d]{0,20}(\d[\d ]*)\s*m²", desc_text, re.IGNORECASE)
    terrain = float(terrain_m.group(1).replace(" ", "")) if terrain_m else None
    dpe = _re_str(r"\bDPE\s*:?\s*(?:classe\s*)?([A-G])\b", desc_text)

    # Ville + CP du BIEN : <div class="trade-location">CHAMPAGNE (72470)</div>
    # (par-card sur la liste : c'est la localisation du bien, pas de l'agence)
    loc_el = card.select_one(".trade-location")
    loc_text = loc_el.get_text(strip=True) if loc_el else ""
    city_m = re.search(r"([A-ZÀ-ÿ][^\d\(]{1,40})\s*\((\d{5})\)", loc_text)
    ville = city_m.group(1).strip() if city_m else ""
    cp = city_m.group(2) if city_m else ""
    # departement dérivé du VRAI code postal (corrigé via détail au besoin)
    departement = cp[:2] if cp else dept

    # Titre
    titre_el = card.select_one(".trade-title, h2, h3")
    type_str = titre_el.get_text(strip=True) if titre_el else "Maison"
    titre = _make_titre(type_str, pieces, ville)

    # Photos : srcset → extraire URLs images.proprietes-privees.com
    photos = []
    for el in card.select("img, source"):
        srcset = el.get("srcset", "") or el.get("src", "")
        for part in srcset.split(","):
            src = part.strip().split(" ")[0]
            orig_m = re.search(r"/_ipx/[^/]+/(https?://[^\s,]+)", src)
            if orig_m:
                clean = orig_m.group(1)
            elif "images.proprietes-privees.com" in src:
                clean = src
            else:
                continue
            if any(e in clean.lower() for e in [".jpg", ".jpeg", ".webp", ".png"]):
                photos.append(clean)
    photos = list(dict.fromkeys(photos))[:10]

    return {
        "source": "proprietes_privees",
        "url": url,
        "id_annonce": str(ad_id),
        "titre": titre,
        "type_bien": "maison",
        "description": desc_text[:1200],
        "departement": departement,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Propriétés Privées",
        "_type_str": type_str,
    }


def _re_float(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text)
    if m:
        try:
            return float(m.group(1).replace(" ", "").replace(",", "."))
        except Exception:
            pass
    return None


def _re_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _re_str(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).upper() if m else None


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria
    criteres = load_criteria()
    biens = asyncio.run(search({
        "departements": criteres.departements[:2],
        "prix_max": criteres.prix_max,
        "prix_min": criteres.prix_min,
        "surface_min": criteres.surface_min,
    }))
    print(f"\nTotal Propriétés Privées: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']} ({b['code_postal']}) dept {b['departement']}")

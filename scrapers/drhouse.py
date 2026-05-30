"""scrapers/drhouse.py — Dr House Immo (réseau de mandataires national)

Méthode : scrape_simple (httpx) — SSR Laravel (pas de JS nécessaire)
URL pattern listing : /annonces/vente/maison/{ville}-{cp}?rayon_localisation=50&page=N
  - le filtre `rayon_localisation` est un rayon (km) autour de la ville-ancre :
    server-side et efficace, mais il DÉBORDE sur les départements voisins
    (ex: Le Mans r=50 ramène quelques biens du 61). On post-filtre donc
    STRICTEMENT par code_postal[:2] (comme remax/era).

Cartes : article.shadow_border
  - URL    : a[href*="/annonce/vente/"]  → contient ville-{cp}/{type}-N-pieces-Mm2/{id}
  - CP     : extrait du slug de l'URL (fiable) — sinon du H2
  - Titre  : h2
  - Prix   : p.font-medium (… €)
  - Chips  : li.ellipsis_text  →  "129 m²", "6 pièces", "3 chambres"
  - Photos : img[src*=images.drhouse-immo.com .../vente/...]

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.drhouse-immo.com"
RAYON_KM = 50          # rayon autour de la ville-ancre (server-side)
MAX_PAGES = 12         # plafond pages / département
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Code département → ville-ancre (slug ville-cp) servant de centre au rayon.
# Préfecture de chaque département cible.
DEPT_ANCHORS: dict[str, str] = {
    "72": "le-mans-72000",
    "28": "chartres-28000",
    "45": "orleans-45000",
    "89": "auxerre-89000",
    "49": "angers-49000",
    "37": "tours-37000",
    "36": "chateauroux-36000",
    "18": "bourges-18000",
    "58": "nevers-58000",
    "41": "blois-41000",
    "53": "laval-53000",
}

# slug d'URL : .../annonce/vente/{ville}-{cp}/{type}-...m2/{id}
_URL_RE = re.compile(
    r"/annonce/vente/(?P<ville>[a-z0-9-]+)-(?P<cp>\d{5})/(?P<typeslug>[a-z0-9-]+?)/(?P<id>\d+)/?$",
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
            anchor = DEPT_ANCHORS.get(dept)
            if not anchor:
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, anchor, departements,
                    prix_max, prix_min, surface_min, seen_ids,
                )
                results.extend(biens)
                print(f"[DrHouse] Dept {dept}: {len(biens)} annonces (en-département)")
            except Exception as e:
                print(f"[DrHouse] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.8)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    anchor: str,
    departements: list[str],
    prix_max: int,
    prix_min: int,
    surface_min: int,
    seen_ids: set[str],
) -> list[dict]:
    biens: list[dict] = []

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/annonces/vente/maison/{anchor}?rayon_localisation={RAYON_KM}"
        if page > 1:
            url += f"&page={page}"

        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = _parse_html(r.text)
        if not cards:
            break

        page_kept = 0
        for bien in cards:
            aid = bien["id_annonce"]
            if aid in seen_ids:
                continue

            # ── POST-FILTRE DÉPARTEMENT STRICT (le rayon déborde sur les voisins) ──
            cp = bien.get("code_postal") or ""
            if departements and cp[:2] not in departements:
                continue
            page_kept += 1

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

        # Dernière page (listing partiel) → on arrête
        if len(cards) < 21:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []
    for card in soup.select("article.shadow_border"):
        try:
            bien = _parse_card(card)
            if bien:
                results.append(bien)
        except Exception:
            continue
    return results


def _parse_card(card) -> dict | None:
    a = card.find("a", href=lambda h: h and "/annonce/vente/" in h)
    if not a:
        return None
    href = a["href"]
    url = href if href.startswith("http") else BASE_URL + href

    m = _URL_RE.search(href)
    if not m:
        return None
    ad_id = m.group("id")
    cp = m.group("cp")
    ville = m.group("ville").replace("-", " ").title()
    type_slug = m.group("typeslug").lower()

    # type de bien
    if "maison" in type_slug:
        type_bien = "maison"
    elif "appartement" in type_slug:
        type_bien = "appartement"
    elif "terrain" in type_slug:
        type_bien = "terrain"
    elif "chateau" in type_slug:
        type_bien = "château"
    else:
        type_bien = type_slug.split("-")[0]

    # titre (h2)
    h2 = card.find("h2")
    titre = h2.get_text(" ", strip=True) if h2 else ""
    if not titre:
        titre = f"{type_bien.capitalize()} — {ville} ({cp})"

    # ── prix : premier <p> contenant '€' sans '€ le m²' ──
    prix = None
    for p_el in card.find_all("p"):
        txt = p_el.get_text(" ", strip=True)
        if "€" in txt and "le m²" not in txt and "/m²" not in txt:
            prix = _parse_price(txt)
            if prix:
                break

    # ── chips li : surface / pièces / chambres ──
    surface = None
    pieces = None
    chambres = None
    for li in card.select("li.ellipsis_text"):
        t = li.get_text(" ", strip=True)
        if surface is None and "m²" in t:
            surface = _parse_surface(t)
        elif pieces is None and "pièce" in t:
            pieces = _parse_int(t)
        elif chambres is None and "chambre" in t:
            chambres = _parse_int(t)

    # fallback surface depuis le slug d'URL (…-129m2/…)
    if surface is None:
        ms = re.search(r"(\d+)m2", type_slug)
        if ms:
            surface = float(ms.group(1))
    # fallback pièces depuis le slug (…-6-pieces-…)
    if pieces is None:
        mp = re.search(r"(\d+)-pieces", type_slug)
        if mp:
            pieces = int(mp.group(1))

    # ── photos ──
    photos = []
    for img in card.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "images.drhouse-immo.com" in src and "/vente/" in src:
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "drhouse",
        "url": url,
        "id_annonce": ad_id,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": titre,
        "departement": cp[:2],
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Dr House Immo",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text.split("le m²")[0])
    cleaned = cleaned.replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", cleaned)
    if m:
        try:
            return float(m.group(0))
        except ValueError:
            return None
    return None


def _parse_surface(text: str) -> float | None:
    m = re.search(r"([\d\s\xa0,\.]+)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
        try:
            return float(val)
        except ValueError:
            return None
    return None


def _parse_int(text: str) -> int | None:
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


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
    print(f"\nTotal Dr House Immo: {len(biens)} annonces")
    by_dept: dict[str, int] = {}
    for b in biens:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print("Par département:", dict(sorted(by_dept.items())))
    for b in biens[:8]:
        print(
            f"  [{b['departement']}] {b['titre'][:60]}"
            f" — {b['prix']}€ — {b.get('surface')}m²"
            f" — {b['ville']} ({b['code_postal']})"
        )

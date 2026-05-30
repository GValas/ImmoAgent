"""scrapers/emile_garcin.py — Emile Garcin (immobilier de prestige : châteaux, propriétés)

Méthode : scrape_simple (httpx) — SSR Symfony
Listing  : /fr/annonces/vente-immobilier?page=N  (~15 cartes/page, ~62 pages = ~920 biens)
Cards    : article.property-card
  - URL    : a.property-card-link  →  /fr/annonce/{slug}
  - Ville  : p.property-card-city
  - Titre  : h3.property-card-title
  - Réf    : p.property-card-reference  →  "Référence : XXX-0000-AA"
  - Infos  : ul.property-card-info  →  "1 890 000 €", "Superficie 305m²", "5 chambres"
  - Photo  : div.property-card-image img[src]

POINT CRITIQUE — filtre département :
  Le filtre serveur (form POST search_result[locations][]) est INUTILISABLE en httpx :
  le jeton CSRF (data-controller="csrf-protection", value="csrf-token") est injecté
  côté client par Stimulus ; sans JS, le POST est ignoré et le serveur renvoie la liste
  nationale par défaut. Les cartes ne contiennent PAS de code postal.

  Stratégie retenue (voie b, comme remax/era) : on crawle l'inventaire national/international
  complet (gérable, ~920 biens), puis on POST-FILTRE par département en résolvant le nom
  de ville via l'autocomplétion officielle du site /fr/load-locations (commune → code postal).
  Le département est donc déterminé à partir du code postal réel de la commune, et seuls les
  biens dans criteres['departements'] sont conservés.

Spécificité : biens de prestige (châteaux, manoirs, hôtels particuliers, propriétés).
Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.emilegarcin.com"
LISTING_URL = f"{BASE_URL}/fr/annonces/vente-immobilier"
LOCATIONS_URL = f"{BASE_URL}/fr/load-locations"

MAX_PAGES = 70          # garde-fou : l'inventaire fait ~62 pages
PHOTOS_PER_CARD = 1     # une seule vignette dispo sur la carte de listing
LOC_CONCURRENCY = 6     # concurrence sur la résolution ville → code postal

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Mots-clés type de bien : exclure appartements/studios (le projet vise des maisons/propriétés)
_EXCLUDE_KEYWORDS = re.compile(
    r"\bappartement\b|\bappart\b|\bstudio\b|\bloft\b|\bduplex\b|\bt[1-6]\b|\bf[1-6]\b",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25) as client:
        # 1) Crawl de l'inventaire national complet
        cards = await _crawl_all(client)
        print(f"[EmileGarcin] {len(cards)} biens dans l'inventaire national")

        # 2) Résolution ville → code postal (autocomplétion du site), avec cache
        villes = {c["ville"] for c in cards if c.get("ville")}
        cp_cache = await _resolve_villes(client, villes)

    # 3) Post-filtrage par département + prix/surface
    results: list[dict] = []
    seen: set[str] = set()
    for c in cards:
        cp = cp_cache.get(c.get("ville", ""))
        dept = cp[:2] if cp and len(cp) >= 2 else None
        if departements and dept not in departements:
            continue

        p = c.get("prix") or 0
        s = c.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        c["code_postal"] = cp or ""
        c["departement"] = dept or ""

        aid = c.get("id_annonce") or c.get("url")
        if aid in seen:
            continue
        seen.add(aid)
        results.append(c)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[EmileGarcin] Dept {dept}: {n} annonces")

    return results


async def _crawl_all(client: httpx.AsyncClient) -> list[dict]:
    """Parcourt toutes les pages du listing national jusqu'à épuisement."""
    cards: list[dict] = []
    seen_refs: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = LISTING_URL if page == 1 else f"{LISTING_URL}?page={page}"
        try:
            r = await client.get(url)
            r.raise_for_status()
        except Exception as e:
            print(f"[EmileGarcin] Erreur page {page}: {e}")
            break

        page_cards = _parse_listing(r.text)
        if not page_cards:
            break

        new = 0
        for c in page_cards:
            ref = c.get("id_annonce") or c.get("url")
            if ref and ref not in seen_refs:
                seen_refs.add(ref)
                cards.append(c)
                new += 1

        # Page sans nouveauté (ex: pagination qui boucle) → on s'arrête
        if new == 0:
            break

        await asyncio.sleep(0.25)

    return cards


def _parse_listing(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []

    for card in soup.select("article.property-card"):
        try:
            bien = _parse_card(card)
            if bien:
                results.append(bien)
        except Exception:
            continue

    return results


def _parse_card(card) -> dict | None:
    # ── URL + slug ───────────────────────────────────────────────────────────
    link = card.select_one("a.property-card-link") or card.find("a", href=True)
    href = link["href"] if link and link.get("href") else ""
    url = href if href.startswith("http") else f"{BASE_URL}{href}" if href else ""

    # ── Titre ────────────────────────────────────────────────────────────────
    title_el = card.select_one("h3.property-card-title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    # Filtre type de bien : on écarte les appartements/studios
    if titre and _EXCLUDE_KEYWORDS.search(titre):
        return None

    # ── Référence (id_annonce) ───────────────────────────────────────────────
    ref_el = card.select_one("p.property-card-reference")
    ref_text = ref_el.get_text(" ", strip=True) if ref_el else ""
    m_ref = re.search(r"Référence\s*:\s*(.+)$", ref_text)
    id_annonce = m_ref.group(1).strip() if m_ref else (url.rsplit("/", 1)[-1] if url else None)

    # ── Ville ────────────────────────────────────────────────────────────────
    city_el = card.select_one("p.property-card-city")
    ville = city_el.get_text(" ", strip=True) if city_el else ""

    # ── Infos : prix, surface, chambres ──────────────────────────────────────
    infos = [li.get_text(" ", strip=True) for li in card.select("li.property-card-info-item")]
    infos_joined = " | ".join(infos)

    prix = _parse_price(infos_joined)
    surface = _parse_surface(infos_joined)
    chambres = _parse_int(r"(\d+)\s*chambres?", infos_joined)

    # ── Photo (vignette) ─────────────────────────────────────────────────────
    photos: list[str] = []
    img = card.select_one("div.property-card-image img[src]")
    if img and img.get("src"):
        src = img["src"]
        photos.append(src if src.startswith("http") else f"{BASE_URL}{src}")

    if not url:
        return None

    return {
        "source": "emile_garcin",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": _guess_type(titre),
        "description": titre,
        "departement": "",       # rempli après résolution code postal
        "ville": ville[:80],
        "code_postal": "",        # rempli après résolution
        "surface": surface,
        "surface_terrain": None,  # non exposé sur la carte de listing
        "pieces": None,           # non exposé (le site donne chambres, pas pièces)
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Emile Garcin",
    }


async def _resolve_villes(client: httpx.AsyncClient, villes: set[str]) -> dict[str, str]:
    """Résout chaque nom de ville en code postal via l'autocomplétion du site.

    Retourne {ville: "75001"}. Les villes non résolues sont absentes du dict.
    """
    cache: dict[str, str] = {}
    sem = asyncio.Semaphore(LOC_CONCURRENCY)

    async def one(ville: str):
        async with sem:
            cp = await _city_to_cp(client, ville)
            if cp:
                cache[ville] = cp

    await asyncio.gather(*(one(v) for v in villes if v))
    return cache


async def _city_to_cp(client: httpx.AsyncClient, ville: str) -> str | None:
    """Interroge /fr/load-locations pour obtenir le code postal d'une commune."""
    # On normalise : "Paris 15" / "Paris 1" → "Paris" pour la recherche
    q = re.sub(r"\s+\d{1,2}$", "", ville).strip()
    if not q:
        return None
    try:
        r = await client.get(
            LOCATIONS_URL,
            params={"q": q},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None

    items = data.get("items", [])
    target = _norm(q)

    # On exige une correspondance EXACTE sur le nom de commune (id "3-...").
    # Pas de fallback "approchant" : des homonymes (ex. "Murs" en 36 ET en 84)
    # provoqueraient des faux positifs hors-département.
    matches = [
        str(it["levenshtein_code"])
        for it in items
        if it.get("id", "").startswith("3-")
        and re.fullmatch(r"\d{5}", str(it.get("levenshtein_code", "")))  # CP FR à 5 chiffres
        and _norm(it.get("levenshtein_text", "")) == target
    ]
    if not matches:
        return None

    # Homonymes multiples : ambigu → on n'attribue pas de département (None)
    # sauf si tous les matches partagent le même département (ex. plusieurs CP d'une
    # même ville comme Tours 37000/37100/37200).
    depts = {cp[:2] for cp in matches}
    if len(depts) == 1:
        return matches[0]
    return None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Minuscule, sans accents, sans tirets/apostrophes — pour comparer des noms de villes."""
    s = s.lower().strip()
    table = str.maketrans("àâäéèêëîïôöùûüç", "aaaeeeeiioouuuc")
    s = s.translate(table)
    return re.sub(r"[^a-z0-9]", "", s)


def _parse_price(text: str) -> float | None:
    """'1 890 000 €' → 1890000.0"""
    m = re.search(r"([\d][\d\s  .]*)\s*€", text)
    if not m:
        return None
    cleaned = re.sub(r"[\s  .]", "", m.group(1))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'Superficie 305m²' / '110 m²' → 305.0"""
    m = re.search(r"([\d\s ]+(?:[.,]\d+)?)\s*m²", text)
    if not m:
        return None
    val = re.sub(r"[\s ]", "", m.group(1)).replace(",", ".")
    try:
        return float(val)
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _guess_type(titre: str) -> str:
    t = (titre or "").lower()
    for kw in ("château", "chateau", "manoir", "moulin", "ferme", "longère", "longere",
               "domaine", "propriété", "propriete", "demeure", "villa", "chalet",
               "hôtel particulier", "hotel particulier", "maison"):
        if kw in t:
            return "maison"
    return "maison"


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
    print(f"\nTotal Emile Garcin (en-département): {len(biens)} annonces")
    for b in biens[:15]:
        print(
            f"  [{b['departement']}] {b['titre'][:60]}"
            f" — {b['prix']}€"
            f" — {b.get('surface', '?')}m²"
            f" — {b['ville']} ({b['code_postal']})"
        )

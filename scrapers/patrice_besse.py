"""
scrapers/patrice_besse.py — Patrice Besse (demeures de caractère rurales)
Méthode : httpx pur — SSR HTML
Approche :
  1. Fetche /recherche-bien-immobilier-france/ (page unique avec toutes les annonces)
  2. Filtre les liens /annonces/... dont le snippet mentionne nos régions cibles
  3. Fetche les fiches individuelles pour extraire prix, surface, localisation
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE = "https://www.patrice-besse.com"

# Mots-clés régionaux → département(s) correspondant(s)
_REGION_KEYWORDS: dict[str, list[str]] = {
    "sarthe":           ["72"],
    "le mans":          ["72"],
    "maine":            ["72", "49", "53"],
    "maine-et-loire":   ["49"],
    "anjou":            ["49"],
    "angers":           ["49"],
    "mayenne":          ["53"],
    "laval":            ["53"],
    "eure-et-loir":     ["28"],
    "chartres":         ["28"],
    "beauce":           ["28"],
    "loiret":           ["45"],
    "orléans":          ["45"],
    "sologne":          ["45", "41"],
    "loir-et-cher":     ["41"],
    "blois":            ["41"],
    "indre-et-loire":   ["37"],
    "touraine":         ["37"],
    "tours":            ["37"],
    "indre":            ["36"],
    "châteauroux":      ["36"],
    "cher":             ["18"],
    "berry":            ["18", "36"],
    "bourges":          ["18"],
    "nièvre":           ["58"],
    "nevers":           ["58"],
    "yonne":            ["89"],
    "auxerre":          ["89"],
    "bourgogne":        ["89", "58"],
    "centre-val-de-loire": ["28", "45", "37", "36", "18", "41"],
    "val de loire":     ["37", "41", "49", "45"],
    "pays de la loire": ["72", "49", "53"],
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Limiter à N fiches individuelles max pour ne pas sur-solliciter le site
MAX_INDIVIDUAL_FETCHES = 40


def _keywords_match(text: str, target_depts: set[str]) -> str | None:
    """Retourne le premier dept matché si le texte mentionne une de nos régions."""
    text_lower = text.lower()
    for keyword, depts in _REGION_KEYWORDS.items():
        if keyword in text_lower:
            for d in depts:
                if d in target_depts:
                    return d
    return None


def _parse_listing_page(html: str, target_depts: set[str]) -> list[dict]:
    """
    Parse la page principale et retourne les (url, snippet, dept_guessed) des biens candidats.
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    seen_urls: set[str] = set()

    for a in soup.select("a[href*='/annonces/']"):
        href = a.get("href", "")
        if not href or href in seen_urls:
            continue
        url = href if href.startswith("http") else BASE + href

        # Texte du lien + celui du parent immédiat
        text = ""
        if a.parent:
            text = a.parent.get_text(" ", strip=True)
        if not text:
            text = a.get_text(" ", strip=True)

        # Aussi le texte de l'URL elle-même (encode souvent la région)
        url_text = href.replace("-", " ").replace("_", " ")

        combined = (text + " " + url_text).lower()
        dept = _keywords_match(combined, target_depts)
        if dept:
            candidates.append({"url": url, "snippet": text[:300], "dept_guessed": dept})
            seen_urls.add(href)

    return candidates


def _parse_fiche(html: str, url: str, dept_guessed: str) -> dict | None:
    """Parse une fiche individuelle Patrice Besse."""
    soup = BeautifulSoup(html, "html.parser")

    # ── Prix : class="prix" sur font ou td ──
    prix = None
    for el in soup.select("font.prix, [class~='prix']"):
        text_prix = el.get_text(" ", strip=True).replace("\xa0", " ")
        m = re.search(r"([\d][\d\s]*\d)\s*€", text_prix)
        if m:
            try:
                prix = float(m.group(1).replace(" ", ""))
                break
            except Exception:
                pass
    if not prix:
        # Fallback : premier grand nombre suivi de €
        m = re.search(r"([\d]{3,}[\d\s]*\d)\s*€", soup.get_text(" ").replace("\xa0", " "))
        if m:
            try:
                prix = float(m.group(1).replace(" ", ""))
            except Exception:
                pass

    if not prix or prix < 10_000:
        return None

    full_text = soup.get_text(" ", strip=True).replace("\xa0", " ")

    # ── Surface habitable ──
    surface = None
    for pat in [
        r"superficie\s+(?:habitable\s+)?(?:de\s+)?(\d+(?:[.,]\d+)?)\s*m²",
        r"(\d+(?:[.,]\d+)?)\s*m²\s+(?:habitables?|de\s+surface)",
        r"surface\s+habitable\s+:?\s*(\d+(?:[.,]\d+)?)\s*m²",
    ]:
        m = re.search(pat, full_text, re.IGNORECASE)
        if m:
            try:
                surface = float(m.group(1).replace(",", "."))
                break
            except Exception:
                pass
    if not surface:
        # Premier m² qui n'est pas un terrain/parc
        for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*m²", full_text, re.IGNORECASE):
            ctx = full_text[max(0, m.start()-50):m.start()].lower()
            if "terrain" not in ctx and "parc" not in ctx and "ha" not in ctx:
                try:
                    v = float(m.group(1).replace(",", "."))
                    if 50 <= v <= 2000:
                        surface = v
                        break
                except Exception:
                    pass

    # ── Terrain (souvent en ha) ──
    terrain = None
    for pat in [
        r"parc\s+(?:de\s+)?(\d+(?:[.,]\d+)?)\s*ha",
        r"(\d+(?:[.,]\d+)?)\s*ha\s+(?:de\s+)?(?:parc|terrain|propriété)",
        r"terrain\s+(?:de\s+)?([\d\s]+)\s*m²",
    ]:
        m = re.search(pat, full_text, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1).replace(",", ".").replace(" ", ""))
                terrain = val * 10_000 if "ha" in m.group(0).lower() else val
                break
            except Exception:
                pass

    # ── Pièces / chambres ──
    pieces = None
    for pat in [r"(\d+)\s*pièces?", r"(\d+)\s*p\.\s"]:
        m = re.search(pat, full_text, re.IGNORECASE)
        if m:
            try:
                pieces = int(m.group(1))
                break
            except Exception:
                pass
    chambres = None
    m = re.search(r"(\d+)\s*ch(?:ambres?)?", full_text, re.IGNORECASE)
    if m:
        try:
            chambres = int(m.group(1))
        except Exception:
            pass

    # ── Localisation (ville / CP) ──
    city_m = re.search(r"([A-ZÀ-Ÿa-zà-ÿ][^(]{2,30})\s*\((\d{5})\)", full_text)
    ville = city_m.group(1).strip()[:80] if city_m else ""
    cp = city_m.group(2) if city_m else ""
    dept = cp[:2] if cp else dept_guessed

    # ── Titre ──
    h1 = soup.select_one("h1, h2, [class*='titre'], [class*='title']")
    titre = (h1.get_text(strip=True) if h1 else "Propriété de caractère")[:150]
    if not titre:
        titre = "Propriété de caractère"

    # ── Photos ──
    photos = []
    for img in soup.select("img"):
        for attr in ("src", "data-src", "data-lazy-src"):
            src = img.get(attr, "")
            if src and src.startswith("http") and any(
                e in src.lower() for e in [".jpg", ".jpeg", ".webp", ".png"]
            ):
                photos.append(src)
                break
    photos = list(dict.fromkeys(photos))[:8]

    # ── DPE ──
    dpe_m = re.search(r"\bDPE\s*:?\s*(?:classe\s*)?([A-G])\b", full_text, re.IGNORECASE)
    dpe = dpe_m.group(1).upper() if dpe_m else None

    # ── Piscine ──
    has_pool = bool(re.search(r"\bpiscine\b", full_text, re.IGNORECASE))

    # ── ID depuis URL ──
    id_m = re.search(r"--pb(\d+)", url)
    ad_id = id_m.group(1) if id_m else url.split("--")[-1][-12:]

    return {
        "source": "patrice_besse",
        "url": url,
        "id_annonce": str(ad_id),
        "titre": titre,
        "type_bien": "maison",
        "description": full_text[:1200],
        "departement": dept,
        "ville": ville,
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Patrice Besse",
        "has_pool": has_pool,
    }


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max    = criteres.get("prix_max", 600_000)
    prix_min    = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 80)

    biens: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=30) as client:
        # ── Étape 1 : page de listing principale ──────────────────────────
        print("[PatriceBesse] Chargement de la page principale (~4 MB)...")
        try:
            r = await client.get(f"{BASE}/recherche-bien-immobilier-france/")
            if r.status_code != 200:
                print(f"[PatriceBesse] Erreur HTTP {r.status_code} sur la page principale")
                return []
        except Exception as e:
            print(f"[PatriceBesse] ERR page principale: {e}")
            return []

        candidates = _parse_listing_page(r.text, departements)
        print(f"[PatriceBesse] {len(candidates)} candidats détectés pour nos régions")

        if not candidates:
            return []

        # ── Étape 2 : fetch des fiches individuelles ──────────────────────
        fetch_limit = min(len(candidates), MAX_INDIVIDUAL_FETCHES)
        if len(candidates) > fetch_limit:
            print(f"[PatriceBesse] Limité à {fetch_limit} fiches (sur {len(candidates)} candidats)")

        for cand in candidates[:fetch_limit]:
            url = cand["url"]
            dept_guessed = cand["dept_guessed"]

            id_m = re.search(r"--pb(\d+)", url)
            ad_id = id_m.group(1) if id_m else url[-12:]
            if ad_id in seen_ids:
                continue

            try:
                r2 = await client.get(url)
                if r2.status_code != 200:
                    continue
                b = _parse_fiche(r2.text, url, dept_guessed)
                if not b:
                    continue

                # Filtre prix / surface
                if prix_max and b.get("prix") and b["prix"] > prix_max:
                    continue
                if prix_min and b.get("prix") and b["prix"] < prix_min:
                    continue
                if surface_min and b.get("surface") and b["surface"] < surface_min:
                    continue

                seen_ids.add(ad_id)
                biens.append(b)
                print(f"[PatriceBesse] ✓ {b['titre'][:60]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")
            except Exception as e:
                print(f"[PatriceBesse] ERR fiche {url}: {e}")

            await asyncio.sleep(0.3)

    print(f"[PatriceBesse] total: {len(biens)} biens")
    return biens


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria
    criteres = load_criteria()

    async def _test():
        result = await search({
            "departements": [72, 53, 49, 37, 41, 45, 28, 36, 18, 58, 89],
            "prix_max": criteres.prix_max,
            "prix_min": criteres.prix_min,
            "surface_min": criteres.surface_min,
        })
        print(f"\nTotal: {len(result)} annonces")
        for b in result[:10]:
            print(
                f"  {b['titre'][:70]} — {b['prix']}€ "
                f"— {b['surface']}m² — {b['ville']} ({b['departement']})"
                f"{'  🏊' if b.get('has_pool') else ''}"
            )

    asyncio.run(_test())

"""scrapers/immotourisme.py — ImmoTourisme (immotourisme.com)

Niche : agence 100 % "biens touristiques" — gîtes, maisons d'hôtes, chambres
d'hôtes, propriétés de charme, campings, hébergements insolites. Couverture
nationale, bonne présence en Centre-Val de Loire (37, 36, 41, 45…).

Méthode : scrape_simple (httpx) — SSR HTML (WordPress + Elementor / JetEngine)

URL pattern (liste par département) :
    /departement/{slug}/        ex: /departement/loiret/
  La page contient DEUX grilles JetEngine (.jet-listing-grid) :
    - GRID 0  → biens du département (filtre serveur, classification du site)
    - GRID 1  → carrousel "tous les biens" national (régions mélangées) → À IGNORER.
  On ne lit donc QUE la première grille (grid 0) pour éviter la fuite nationale.

  Les cartes de la liste ne portent que la *région* (Centre-Val de Loire…), pas
  le code postal ni le département → on récupère l'URL détail de chaque carte et
  on lit la page détail.

Page détail (/annonces/{slug}/) :
    - Département : classe CSS du post → "_departement-{slug}" (ex: _departement-loiret)
                   → reverse-map slug → code (DEPT_SLUGS). C'est le signal de
                   filtre dept STRICT.
    - Adresse     : <h2> "Adresse du bien à vendre : {CP} {Ville}" (CP pas toujours présent)
    - Titre       : <h1>
    - Prix        : 1er .jet-listing-dynamic-field__content contenant "€"
    - Photos      : img wp-content/uploads/.../{REF}_{n}_original-*.jpg
    - Type/desc   : déduit du titre + description ; surface best-effort depuis le texte.

Filtre département : POST-FILTRE STRICT sur le slug "_departement-{slug}" de la
page détail (== département demandé) → 0 fuite. Le CP (h2) sert de code_postal
quand il est présent, et de double-vérification CP[:2] le cas échéant.

Couverture observée (2026-06-08) : petit inventaire mais réel — loiret 45: ~4,
indre-et-loire 37: ~9, indre 36 / loir-et-cher 41: présents ; sarthe 72: ~2,
yonne 89: ~1 ; eure-et-loir 28: 0.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.immotourisme.com"
DETAIL_CONCURRENCY = 6
PHOTOS_PER_CARD = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Code département → slug URL /departement/{slug}/  (= classe _departement-{slug})
DEPT_SLUGS: dict[str, str] = {
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
# Reverse : slug → code (pour lire la classe _departement-{slug} de la page détail)
SLUG_TO_DEPT: dict[str, str] = {v: k for k, v in DEPT_SLUGS.items()}

# Type de bien déduit du titre (niche tourisme)
_TYPE_PATTERNS = [
    (re.compile(r"maison\s+d.?h[oô]tes", re.I), "maison d'hôtes"),
    (re.compile(r"chambres?\s+d.?h[oô]tes", re.I), "chambres d'hôtes"),
    (re.compile(r"\bg[iî]tes?\b", re.I), "gîte"),
    (re.compile(r"\bcamping\b", re.I), "camping"),
    (re.compile(r"insolite", re.I), "hébergement insolite"),
    (re.compile(r"ch[aâ]teau", re.I), "château"),
    (re.compile(r"manoir", re.I), "manoir"),
    (re.compile(r"domaine", re.I), "domaine"),
    (re.compile(r"propri[eé]t[eé]", re.I), "propriété"),
    (re.compile(r"\bmas\b", re.I), "mas"),
    (re.compile(r"\bmaison\b", re.I), "maison"),
]


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, slug, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[ImmoTourisme] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[ImmoTourisme] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    slug: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    url = f"{BASE_URL}/departement/{slug}/"
    r = await client.get(url)
    if r.status_code != 200:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    grids = soup.select(".jet-listing-grid")
    if not grids:
        return []

    # GRID 0 = biens du département ; les grilles suivantes = carrousels nationaux.
    detail_urls: list[str] = []
    for it in grids[0].select(".jet-listing-grid__item"):
        for a in it.find_all("a", href=True):
            href = a.get("href", "")
            if "/annonces/" in href and not href.rstrip("/").endswith("/annonces"):
                full = href if href.startswith("http") else BASE_URL + href
                detail_urls.append(full)
                break
    detail_urls = list(dict.fromkeys(detail_urls))

    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def _fetch(u: str) -> dict | None:
        async with sem:
            try:
                rd = await client.get(u)
            except Exception:
                return None
            await asyncio.sleep(0.3)
            if rd.status_code != 200:
                return None
            return _parse_detail(rd.text, u, dept)

    parsed = await asyncio.gather(*[_fetch(u) for u in detail_urls])

    biens: list[dict] = []
    seen: set[str] = set()
    for bien in parsed:
        if not bien:
            continue
        # FILTRE DEPT STRICT : la page détail doit appartenir au département demandé
        if bien.get("departement") != dept:
            continue
        # Double sécurité si CP connu
        cp = bien.get("code_postal") or ""
        if cp and cp[:2] != dept:
            continue

        if bien["id_annonce"] in seen:
            continue

        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        seen.add(bien["id_annonce"])
        biens.append(bien)

    return biens


def _parse_detail(html: str, url: str, dept_attendu: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")

    # On ne garde que les annonces de VENTE (classe _type-annonce-vente).
    # Les autres (_type-annonce-demande-de-gerance, recrutement…) sont écartées.
    if "_type-annonce-vente" not in html:
        return None

    # Département via classe _departement-{slug} sur le post (signal de filtre)
    m_dep = re.search(r"_departement-([a-z-]+)", html)
    dept = SLUG_TO_DEPT.get(m_dep.group(1)) if m_dep else None
    if not dept:
        # pas de département identifiable (ex. annonce de gérance/recrutement) → ignorer
        return None

    # Titre
    h1 = soup.find("h1")
    titre = h1.get_text(" ", strip=True) if h1 else ""

    # Adresse : <h2> "Adresse du bien à vendre : {CP} {Ville}"
    code_postal = ""
    ville = ""
    m_adr = re.search(
        r"à vendre\s*:\s*</span>\s*(\d{5})\s+([^<]+?)\s*</h2>", html
    )
    if not m_adr:
        m_adr = re.search(r"(\d{5})\s+([A-Za-zÀ-ÿ' \-]{2,40})\s*</h2>", html)
    if m_adr:
        code_postal = m_adr.group(1)
        ville = m_adr.group(2).strip()

    # Prix : 1er champ dynamique contenant €
    prix = None
    for el in soup.select(".jet-listing-dynamic-field__content"):
        t = el.get_text(" ", strip=True)
        if "€" in t:
            prix = _parse_price(t)
            if prix:
                break

    # Description (1er bloc de présentation ImmoTourisme)
    description = ""
    for el in soup.select(".jet-listing-dynamic-field__content"):
        t = el.get_text(" ", strip=True)
        if "ImmoTourisme" in t or len(t) > 120:
            description = t
            break

    # Type de bien depuis le titre
    type_bien = "propriété touristique"
    for pat, label in _TYPE_PATTERNS:
        if pat.search(titre):
            type_bien = label
            break

    # Photos & référence : les images d'annonce sont des uploads
    # ".../{epoch}_{REF}_{n}_original-*.jpg", embarquées dans des <img> ET dans
    # des blocs <style>background-image:url(...)</style>. Le bas de page contient
    # un carrousel d'annonces liées (autres refs) → on ne garde que les photos de
    # la référence DOMINANTE, qui est celle de l'annonce courante.
    all_photos = re.findall(
        r"https://www\.immotourisme\.com/wp-content/uploads/"
        r"[^\"')\s]*?_original[^\"')\s]*?\.(?:jpg|jpeg|png|webp)",
        html,
        re.IGNORECASE,
    )
    ref_re = re.compile(r"_([A-Za-z]{1,3}\d{2,6})_\d+_original")
    ref_counts: dict[str, int] = {}
    for p in all_photos:
        m = ref_re.search(p)
        if m:
            ref_counts[m.group(1)] = ref_counts.get(m.group(1), 0) + 1
    ref = max(ref_counts, key=ref_counts.get) if ref_counts else ""

    photos: list[str] = []
    if ref:
        for p in all_photos:
            if f"_{ref}_" in p:
                photos.append(p)
    else:
        photos = all_photos
    photos = list(dict.fromkeys(photos))[:PHOTOS_PER_CARD]

    # id_annonce : ref de l'annonce (stable) sinon slug d'URL
    slug_id = url.rstrip("/").split("/annonces/")[-1]
    id_annonce = ref or slug_id

    # Surface habitable best-effort depuis le texte
    surface = _parse_surface(titre) or _parse_surface(description)
    surface_terrain = _parse_terrain(titre) or _parse_terrain(description)

    # Pièces / chambres best-effort
    chambres = _parse_int(r"(\d+)\s*chambres?", description) or _parse_int(
        r"(\d+)\s*chambres?", titre
    )
    pieces = _parse_int(r"(\d+)\s*pi[eè]ces?", description)

    return {
        "source": "immotourisme",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "ImmoTourisme",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        v = float(cleaned) if cleaned else None
        # garde-fou : prix immobilier plausible
        return v if v and v >= 1000 else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    if not text:
        return None
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_surface(text: str) -> float | None:
    """Cherche 'NNN m²' (habitable plausible) dans le texte."""
    if not text:
        return None
    for m in re.finditer(r"(\d[\d\s\xa0]*)\s*m²", text):
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 20 <= f <= 2000:
                return f
        except ValueError:
            continue
    return None


def _parse_terrain(text: str) -> float | None:
    """'1 hectare', '3,5 ha', '14ha' → m²."""
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:hectares?|ha)\b", text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(",", ".")) * 10000
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
    print(f"\nTotal ImmoTourisme: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus (classe) : {depts}")
    cp_depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus (CP)     : {cp_depts}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}/{b['code_postal'] or '?????'}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

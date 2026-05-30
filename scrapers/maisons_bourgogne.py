"""scrapers/maisons_bourgogne.py — Maisons Bourgogne / Agence PRUNIER (Autun, Morvan)

Méthode : scrape_simple (httpx) — SSR HTML (vieux CMS PHP « index.php?action=... »).
Petite agence locale (Autun / Morvan / Nièvre) : ~38-48 annonces au TOTAL, pas
de filtre département serveur. On scrape l'INTÉGRALITÉ du listing national
(4 pages, 12 cartes/page) puis on POST-FILTRE par département.

Particularité : aucune carte du listing ni la page détail n'exposent de code
postal exploitable (le seul CP présent — 71400 — est l'adresse de l'agence, pas
du bien). Le secteur affiché dans l'en-tête est un nom de zone ("Est Morvan",
"Sud Morvan"...), pas une commune. La SEULE source fiable de commune est le slug
d'URL de la fiche :
    /fr/annonces-immobilieres/offre/{ville-slug}/bien/{id}/{titre-slug}.html
On résout {ville-slug} → département via l'API officielle geo.api.gouv.fr
(gratuite, sans clé), ce qui donne aussi le code postal. C'est ce qui garantit
0 fuite hors-département (les noms de zone "Morvan" chevauchent 58/71/21).

Listing : https://maisons-bourgogne.fr/index.php?action=list
Pagination : index.php?page=N&action=list
Cartes : div.media (chacune contient div.card image + div.media-body infos)
  - en-tête  : p → "Maison - Biens AV - Est Morvan  - Ref : 5305"  (type + réf)
  - titre/url: h2 a.fs-18[href]   (URL fiche SEO, contient le slug ville)
  - description : p.mxw-571
  - critères : ul.list-inline li  → chambres / terrain m² / surface m²
  - prix     : span "Prix : 142 000 €"
  - photo    : img.card-img[src]

Couverture cible : l'agence est centrée sur Autun (71, hors cible) ; seuls les
biens des communes du Sud/Centre Morvan situées en Nièvre (58, CIBLE) sont
retenus (Luzy, Villapourçon, Arleuf, Poil...). Volume faible mais réel.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://maisons-bourgogne.fr"
LISTING_URL = f"{BASE_URL}/index.php?action=list"
GEO_API = "https://geo.api.gouv.fr/communes"
# Aire d'activité réelle de l'agence (Autun / Morvan). On RESTREINT la
# géo-résolution à ces départements adjacents pour éviter les HOMONYMES
# lointains (ex : "Auxy" existe en 71 *et* en 45 ; sans restriction l'API,
# boostée par population, renverrait le 45 → fausse fuite). Une commune dont
# le slug ne tombe dans aucun de ces départements est écartée.
AGENCE_DEPTS = ["58", "71", "21", "89"]
MAX_PAGES = 8          # plafond de sécurité (~4 pages réelles)
PHOTOS_PER_CARD = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Premier segment de l'en-tête → on ne garde que maisons / propriétés / fermes...
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|pavillon|villa|ferme|fermette|longere|longère|"
    r"manoir|ch[âa]teau|moulin|demeure|domaine|mas|g[îi]te|h[ôo]tel particulier|"
    r"ma[îi]tre|corps de ferme|maison de village",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|terrain|local|commerce|murs|garage|parking|immeuble|"
    r"bureau|fonds|viager",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    results: list[dict] = []
    seen: set[str] = set()
    geo_cache: dict[str, tuple[str, str]] = {}  # slug → (dept, code_postal)

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        raw_biens = await _fetch_all_cards(client)

        for bien in raw_biens:
            slug = bien.pop("_ville_slug", "")
            if not slug:
                continue

            dept, code_postal, ville_propre = await _resolve_commune(
                client, slug, geo_cache
            )
            if not dept:
                continue  # commune non résolue → on ne devine pas le dept

            # POST-FILTRE département (0 fuite : dept vient de l'API officielle)
            if departements and dept not in departements:
                continue

            bien["departement"] = dept
            bien["code_postal"] = code_postal
            if ville_propre:
                bien["ville"] = ville_propre

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            aid = bien.get("id_annonce") or bien.get("url")
            if aid in seen:
                continue
            seen.add(aid)
            results.append(bien)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[MaisonsBourgogne] Dept {dept}: {n} annonces")

    return results


async def _fetch_all_cards(client: httpx.AsyncClient) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/index.php?page={page}&action=list"
        try:
            r = await client.get(url)
            if r.status_code != 200:
                break
        except Exception as e:
            print(f"[MaisonsBourgogne] Erreur page {page}: {e}")
            break

        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("div.media")
        page_new = 0
        for card in cards:
            bien = _parse_card(card)
            if not bien:
                continue
            aid = bien.get("id_annonce") or bien.get("url")
            if aid in seen_ids:
                continue
            seen_ids.add(aid)
            biens.append(bien)
            page_new += 1

        # Plus de nouvelles cartes → dernière page atteinte
        if page_new == 0:
            break

        await asyncio.sleep(0.4)

    return biens


def _parse_card(card) -> dict | None:
    body = card.select_one("div.media-body")
    if not body:
        return None

    a = body.select_one("h2 a[href]") or body.select_one("a[href*='/bien/']")
    if not a or not a.get("href"):
        return None
    url = a["href"].strip()
    if url.startswith("/"):
        url = BASE_URL + url

    m = re.search(r"/offre/([^/]+)/bien/(\d+)/", url)
    if not m:
        return None
    ville_slug = m.group(1)
    id_num = m.group(2)

    # En-tête : "Maison - Biens AV - Est Morvan  - Ref : 5305"
    head = body.find("p")
    head_txt = head.get_text(" ", strip=True) if head else ""
    type_seg = head_txt.split("-")[0].strip() if head_txt else ""
    if not type_seg:
        type_seg = ville_slug.replace("-", " ")
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg.lower()

    m_ref = re.search(r"Ref\s*:\s*([\w\-]+)", head_txt, re.IGNORECASE)
    ref = m_ref.group(1) if m_ref else ""
    id_annonce = ref or id_num

    titre = a.get_text(" ", strip=True)
    titre = re.sub(r"\s+", " ", titre).strip()

    desc_el = body.select_one("p.mxw-571") or body.select_one("p.black")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Critères : ul.list-inline li (chambres / terrain / surface habitable)
    pieces = chambres = None
    surface = surface_terrain = None
    crit_ul = body.select_one("ul.list-inline")
    if crit_ul:
        for li in crit_ul.select("li"):
            txt = li.get_text(" ", strip=True)
            cls = " ".join(li.get("class", []))
            svg = li.find("svg")
            icon = ""
            if svg:
                use = svg.find("use")
                icon = (use.get("xlink:href") or use.get("href") or "") if use else ""
            i_el = li.find("i")
            iconcls = " ".join(i_el.get("class", [])) if i_el else ""

            num = _parse_num(txt)
            if num is None:
                continue
            if "bedroom" in icon or "bed" in iconcls:
                chambres = int(num)
            elif "fa-tree" in iconcls or "tree" in icon or "terrain" in cls.lower():
                surface_terrain = num
            elif "square" in icon or "surface" in cls.lower():
                if 8 <= num <= 3000:
                    surface = num

    # Prix : span "Prix : 142 000 €"
    prix = None
    for span in body.find_all("span"):
        t = span.get_text(" ", strip=True)
        if "Prix" in t or "€" in t:
            prix = _parse_num(t)
            if prix:
                break

    # Photo de couverture
    photos = []
    img = card.select_one("img.card-img") or card.select_one("img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src.startswith("/"):
            src = BASE_URL + src
        if src.startswith("http"):
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    ville_fallback = ville_slug.replace("-", " ").title()

    return {
        "source": "maisons_bourgogne",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": "",          # rempli après géo-résolution
        "ville": ville_fallback,
        "code_postal": "",          # rempli après géo-résolution
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "dpe": None,
        "photos": photos,
        "agence": "Agence Prunier",
        "_ville_slug": ville_slug,
    }


async def _resolve_commune(
    client: httpx.AsyncClient,
    slug: str,
    cache: dict[str, tuple[str, str]],
) -> tuple[str, str, str]:
    """slug commune → (departement, code_postal, nom_propre) via geo.api.gouv.fr.

    Retourne ('', '', '') si la commune n'est pas résolue (on n'invente pas
    le département → pas de fuite possible).
    """
    if slug in cache:
        dept, cp, nom = cache[slug]
        return dept, cp, nom

    nom = slug.replace("-", " ")
    norm = _norm(nom)
    best: tuple[str, str, str] | None = None  # (dept, cp, nom_propre)

    # On interroge l'API en se restreignant aux départements de l'agence,
    # et on exige une correspondance EXACTE du nom de commune (après
    # normalisation accents/casse) pour écarter les communes "proches".
    for dept_q in AGENCE_DEPTS:
        params = {
            "nom": nom,
            "codeDepartement": dept_q,
            "fields": "codeDepartement,nom,codesPostaux",
            "limit": "5",
        }
        try:
            r = await client.get(GEO_API, params=params, timeout=15)
            data = r.json() if r.status_code == 200 else []
        except Exception:
            data = []

        for rec in data:
            if _norm(str(rec.get("nom") or "")) != norm:
                continue
            d = str(rec.get("codeDepartement") or "")
            cps = rec.get("codesPostaux") or []
            cp = cps[0] if cps else ""
            best = (d, cp, str(rec.get("nom") or ""))
            break
        if best:
            break

    if not best:
        cache[slug] = ("", "", "")
        return "", "", ""

    cache[slug] = best
    return best


def _norm(s: str) -> str:
    """Normalise pour comparaison de noms de commune : minuscules, sans accents,
    sans tirets/apostrophes ('Larochemillay' vs 'la-rochemillay')."""
    import unicodedata

    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[\s\-']", "", s)
    return s


def _parse_num(text: str) -> float | None:
    """'142 000 €' / '1.470 m²' / '166 m²' → float (sépare le séparateur de milliers)."""
    if not text:
        return None
    t = text.replace("\xa0", " ")
    # Retire l'unité et garde chiffres + séparateurs
    m = re.search(r"([\d][\d\s.,]*)", t)
    if not m:
        return None
    raw = m.group(1).strip()
    # '1.470' (FR milliers) ou '142 000' → on retire . , et espaces de milliers
    cleaned = re.sub(r"[\s.,]", "", raw)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


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
    print(f"\nTotal Maisons Bourgogne (depts cibles): {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

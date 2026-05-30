"""scrapers/terresetdemeuresdefrance.py — Terres & Demeures de France

Méthode : scrape_simple (httpx) — SSR HTML, pas de JS.
Biens de caractère : châteaux, manoirs, maisons de maître, longères, domaines.

Inventaire NATIONAL très réduit (~130 biens). Pas de filtre département
exploitable côté serveur (les listings sont par région, sans CP ni ville
dans les cartes). On récupère donc l'inventaire complet via le listing
"toutes régions / toutes catégories" puis on POST-FILTRE par département.

Listing : /recherche-immobilier.c-0.0.0.0.{page}.html   (10 biens/page, ~13 pages)
Cartes   : <article itemscope itemtype="schema.org/Product">
            - lien fiche : a href "...b-{ID}.html"
            - les cartes ne contiennent NI ville NI code postal.
Fiche    : /{titre}.b-{ID}.html
            - <strong>Département :</strong> {nom français}   (ex: "Indre-et-Loire",
              "Cher - Indre" pour multi-départements)  → seule source fiable du dept
            - <strong>Surface habitable :</strong> 400 m2
            - <strong>Surface terrain :</strong> 8,5 ha | 11928 m2
            - <strong>Nombre de chambres :</strong> 6
            - <strong>Type de bien :</strong> ...
            - Prix : 1 799 865 €
            - Réf : 37_25709
            - photos : /images/fichier_photo_*.jpg

Le dept étant un NOM français, on le mappe vers le code via DEPT_NAME_TO_CODE.
Pas de ville/CP exposés → code_postal=None, ville=None.
Pas de DPE ni de nb de pièces → None.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx

BASE_URL = "https://www.terresetdemeuresdefrance.com"
LISTING_URL = BASE_URL + "/recherche-immobilier.c-0.0.0.0.{page}.html"

MAX_PAGES = 20            # garde-fou (inventaire réel ~13 pages)
PHOTOS_PER_FICHE = 10
FICHE_CONCURRENCY = 8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Nom de département français (tel qu'affiché sur la fiche) → code INSEE.
# Couvre les départements cibles + voisins fréquents de l'inventaire.
DEPT_NAME_TO_CODE = {
    "ain": "01", "aisne": "02", "allier": "03", "alpes-de-haute-provence": "04",
    "hautes-alpes": "05", "alpes-maritimes": "06", "ardeche": "07", "ardennes": "08",
    "ariege": "09", "aube": "10", "aude": "11", "aveyron": "12",
    "bouches-du-rhone": "13", "calvados": "14", "cantal": "15", "charente": "16",
    "charente-maritime": "17", "cher": "18", "correze": "19", "corse": "20",
    "cote-d-or": "21", "cotes-d-armor": "22", "creuse": "23", "dordogne": "24",
    "doubs": "25", "drome": "26", "eure": "27", "eure-et-loir": "28",
    "finistere": "29", "gard": "30", "haute-garonne": "31", "gers": "32",
    "gironde": "33", "herault": "34", "ille-et-vilaine": "35", "indre": "36",
    "indre-et-loire": "37", "isere": "38", "jura": "39", "landes": "40",
    "loir-et-cher": "41", "loire": "42", "haute-loire": "43", "loire-atlantique": "44",
    "loiret": "45", "lot": "46", "lot-et-garonne": "47", "lozere": "48",
    "maine-et-loire": "49", "manche": "50", "marne": "51", "haute-marne": "52",
    "mayenne": "53", "meurthe-et-moselle": "54", "meuse": "55", "morbihan": "56",
    "moselle": "57", "nievre": "58", "nord": "59", "oise": "60", "orne": "61",
    "pas-de-calais": "62", "puy-de-dome": "63", "pyrenees-atlantiques": "64",
    "hautes-pyrenees": "65", "pyrenees-orientales": "66", "bas-rhin": "67",
    "haut-rhin": "68", "rhone": "69", "haute-saone": "70", "saone-et-loire": "71",
    "sarthe": "72", "savoie": "73", "haute-savoie": "74", "paris": "75",
    "seine-maritime": "76", "seine-et-marne": "77", "yvelines": "78",
    "deux-sevres": "79", "somme": "80", "tarn": "81", "tarn-et-garonne": "82",
    "var": "83", "vaucluse": "84", "vendee": "85", "vienne": "86",
    "haute-vienne": "87", "vosges": "88", "yonne": "89", "territoire-de-belfort": "90",
    "essonne": "91", "hauts-de-seine": "92", "seine-saint-denis": "93",
    "val-de-marne": "94", "val-d-oise": "95",
}


def _slugify_dept(name: str) -> str:
    """'Indre-et-Loire' / 'Côte-d'Or' → clé normalisée de DEPT_NAME_TO_CODE."""
    s = name.strip().lower()
    repl = (
        ("à", "a"), ("â", "a"), ("ä", "a"),
        ("é", "e"), ("è", "e"), ("ê", "e"), ("ë", "e"),
        ("î", "i"), ("ï", "i"), ("ô", "o"), ("ö", "o"),
        ("ù", "u"), ("û", "u"), ("ü", "u"), ("ç", "c"),
    )
    for a, b in repl:
        s = s.replace(a, b)
    s = s.replace("'", "-").replace("’", "-").replace(" ", "-")
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def _dept_codes_from_name(raw: str) -> list[str]:
    """Le champ peut lister plusieurs départements : 'Cher - Indre' → ['18','36'].

    On découpe sur ' - ' (séparateur multi-dept) mais on préserve les noms
    composés à trait d'union (Indre-et-Loire, Côte-d'Or) en testant d'abord
    le nom complet.
    """
    raw = raw.strip()
    if not raw:
        return []
    codes: list[str] = []
    # 1) tentative nom complet (cas mono-département composé)
    full = DEPT_NAME_TO_CODE.get(_slugify_dept(raw))
    if full:
        return [full]
    # 2) multi-départements séparés par ' - '
    for part in re.split(r"\s+-\s+", raw):
        code = DEPT_NAME_TO_CODE.get(_slugify_dept(part))
        if code and code not in codes:
            codes.append(code)
    return codes


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        fiche_urls = await _collect_fiche_urls(client)
        print(f"[TDDF] {len(fiche_urls)} biens dans l'inventaire national")

        sem = asyncio.Semaphore(FICHE_CONCURRENCY)

        async def worker(bid: str, url: str):
            async with sem:
                return await _fetch_fiche(client, bid, url, departements)

        biens = await asyncio.gather(
            *(worker(bid, url) for bid, url in fiche_urls.items())
        )

    results: list[dict] = []
    for b in biens:
        if not b:
            continue
        p = b.get("prix") or 0
        s = b.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue
        results.append(b)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[TDDF] Dept {dept}: {n} annonces")

    return results


async def _collect_fiche_urls(client: httpx.AsyncClient) -> dict[str, str]:
    """Parcourt les pages du listing national et collecte {id: url_fiche}."""
    fiches: dict[str, str] = {}
    for page in range(1, MAX_PAGES + 1):
        url = LISTING_URL.format(page=page)
        try:
            r = await client.get(url)
            if r.status_code != 200:
                break
        except Exception as e:
            print(f"[TDDF] Erreur listing page {page}: {e}")
            break

        page_map = {
            bid: u
            for u, bid in re.findall(
                r'href="(' + re.escape(BASE_URL) + r'/[^"]*?\.b-(\d+)\.html)"',
                r.text,
            )
        }

        new = {bid: u for bid, u in page_map.items() if bid not in fiches}
        if not new:
            break
        fiches.update(new)
        await asyncio.sleep(0.3)

    return fiches


async def _fetch_fiche(
    client: httpx.AsyncClient, bid: str, url: str, departements: list[str]
) -> dict | None:
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return None
        html = r.text
    except Exception:
        return None

    dept_raw = _field(html, "Département")
    if not dept_raw:
        return None  # pas de dept → impossible de filtrer de façon fiable
    codes = _dept_codes_from_name(dept_raw)
    if not codes:
        return None
    # POST-FILTRE département : au moins un des départements du bien est ciblé
    in_target = [c for c in codes if c in departements] if departements else codes
    if not in_target:
        return None
    dept_code = in_target[0]

    titre = _title(html)
    type_bien_raw = _field(html, "Type de bien") or ""
    description = _description(html)
    surface = _parse_surface_hab(_field(html, "Surface habitable") or "")
    surface_terrain = _parse_surface_terrain(_field(html, "Surface terrain") or "")
    chambres = _parse_int(_field(html, "Nombre de chambres") or "")
    prix = _parse_price(html)
    photos = _photos(html)

    return {
        "source": "terresetdemeuresdefrance",
        "url": url,
        "id_annonce": bid,
        "titre": titre[:150] if titre else f"Bien {bid}",
        "type_bien": _type_bien(type_bien_raw),
        "description": description,
        "departement": dept_code,
        "ville": None,
        "code_postal": None,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "dpe": None,
        "photos": photos,
        "agence": "Terres & Demeures de France",
    }


# ── Helpers de parsing ─────────────────────────────────────────────────────────

def _field(html: str, label: str) -> str | None:
    """Récupère le texte d'un <li><strong>{label} :</strong> VALEUR</li>."""
    m = re.search(
        re.escape(label) + r"\s*:\s*</strong>\s*(.*?)\s*</li>",
        html,
        re.S | re.I,
    )
    if not m:
        return None
    val = re.sub(r"<[^>]+>", " ", m.group(1))
    val = re.sub(r"\s+", " ", val).strip()
    return val or None


def _title(html: str) -> str | None:
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
    if m:
        t = re.sub(r"<[^>]+>", " ", m.group(1))
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            return t
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).split("|")[0].strip()
    return None


def _description(html: str) -> str:
    m = re.search(r'itemprop="description"[^>]*>(.*?)</', html, re.S | re.I)
    if not m:
        return ""
    txt = re.sub(r"<[^>]+>", " ", m.group(1))
    txt = re.sub(r"&[a-z]+;", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:1200]


def _type_bien(raw: str) -> str:
    # Le champ "Type de bien" est un libellé de catégorie large (il peut
    # énumérer plusieurs types). On privilégie les biens de caractère ;
    # 'appartement' n'est retenu que si AUCUN type maison/demeure n'apparaît.
    low = raw.lower()
    if "château" in low or "chateau" in low or "manoir" in low:
        return "château/manoir"
    if "domaine" in low or "propriété" in low or "propriete" in low:
        return "domaine"
    house = any(
        kw in low
        for kw in (
            "maison", "logis", "demeure", "moulin", "longère", "longere",
            "hôtel particulier", "hotel particulier", "ferme", "villa",
        )
    )
    if house:
        return "maison"
    if "appartement" in low:
        return "appartement"
    return "maison"


def _parse_price(html: str) -> float | None:
    m = re.search(r"Prix\s*:\s*([0-9\s  ]+)\s*€", html)
    if not m:
        m = re.search(r'itemprop="price"[^>]*>[^0-9]*([0-9\s  ]+)', html)
    if not m:
        return None
    cleaned = re.sub(r"[\s  ]", "", m.group(1))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface_hab(text: str) -> float | None:
    """'400 m2' / '343 m²' / '400 m2 maison de gardien 110 m2' → 1ère valeur."""
    m = re.search(r"([\d\s ]+)\s*m[²2]", text)
    if not m:
        return None
    val = re.sub(r"[\s ]", "", m.group(1))
    try:
        return float(val)
    except ValueError:
        return None


def _parse_surface_terrain(text: str) -> float | None:
    """'8,5 ha' / '11928 m2' → m²."""
    m_ha = re.search(r"([\d,\.]+)\s*ha", text, re.I)
    if m_ha:
        try:
            return float(m_ha.group(1).replace(",", ".")) * 10000
        except ValueError:
            pass
    m = re.search(r"([\d\s ]+)\s*m[²2]", text)
    if m:
        val = re.sub(r"[\s ]", "", m.group(1))
        try:
            return float(val)
        except ValueError:
            pass
    return None


def _parse_int(text: str) -> int | None:
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def _photos(html: str) -> list[str]:
    photos: list[str] = []
    seen: set[str] = set()
    for path in re.findall(r'src="(/images/[^"]+\.jpg)"', html):
        # version pleine résolution : retire le préfixe 'mini_'
        full = path.replace("/mini_", "/")
        if full not in seen:
            seen.add(full)
            photos.append(BASE_URL + full)
        if len(photos) >= PHOTOS_PER_FICHE:
            break
    return photos


# ── CLI standalone ─────────────────────────────────────────────────────────────

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
    print(f"\nTotal TDDF: {len(biens)} annonces en-département")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:60]}"
            f" — {b['prix']}€"
            f" — {b.get('surface')}m²"
            f" — {b.get('surface_terrain')}m² terrain"
            f" — {b['type_bien']}"
            f" — {len(b['photos'])} photos"
        )

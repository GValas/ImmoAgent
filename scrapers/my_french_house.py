"""scrapers/my_french_house.py — My French House (my-french-house.com)

Portail acheteurs anglophones, biens de caractère (manoirs, longères, châteaux,
fermes, propriétés). Forte présence Loire Valley / Centre / Pays-de-la-Loire.

Méthode : scrape_simple (httpx) — SSR HTML (Tailwind/Alpine, CDN BunnyCDN).
URL listing par DÉPARTEMENT (slug serveur, FIABLE) :
    /property-for-sale-in-france/in/{dept-slug}
  ex: /property-for-sale-in-france/in/sarthe , /in/eure-et-loir , /in/loiret

Filtre département : CÔTÉ SERVEUR via le slug. La liste contient en plus 1-4
cartes « featured » d'autres départements (ex. Alpes-Maritimes, Aude). On les
écarte par POST-FILTRE : le nom de département de la carte doit matcher la cible.
Le nom de dept apparaît à deux endroits concordants :
  - dans le slug de l'URL fiche : /property-in-france/{ville}-{dept}-{region}/{type}/{id}
  - dans la ligne texte "Ville, Département" de la carte.

PAS DE CODE POSTAL sur le site (seulement nom de ville + nom de département).
→ code_postal laissé vide ; departement déduit du nom (mapping ci-dessous).
  La géoloc (scrapers/geolocate.py) peut compléter via le nom de commune.

Pagination : `?page=N` existe mais ne pagine PAS en SSR (page 2 == page 1,
~24 cartes max rendues côté serveur). Pour les départements cibles le stock est
faible (1-7 biens), il tient intégralement dans la première page → une requête/dept.

Cartes : div.group.flex.flex-col (attribut x-data="propertyCard({id:...})")
  - URL/id : a[href*="/property-in-france/"] + id dans x-data / fin d'URL
  - Réf    : span "Ref: MFH-xxx"
  - Titre  : h3 > a[title]
  - Prix   : "€1,248,000" (incl. fees)
  - Type   : "{Type} for Sale in" (Farmhouse / Chateau / House / Countryside house…)
  - Loc    : "Ville, Département"
  - Specs  : "{beds}" puis "{surface} m²" puis "{terrain} ha"
  - Photos : img[src*="/properties/{id}/"] (BunnyCDN)

Couverture : faible mais réelle sur la zone (sarthe 1, eure-et-loir 1, loiret 1,
yonne 3, indre 5, nievre 6 ; 37/18/41/49/53 souvent 0). Biens de prestige.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.my-french-house.com"
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Code département → slug listing my-french-house.com/.../in/{slug}
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

# Nom de département (normalisé, sans accents/tirets) → code, pour le post-filtre.
# Le nom vient soit du slug d'URL fiche, soit de la ligne "Ville, Département".
DEPT_NAMES: dict[str, str] = {
    "sarthe": "72",
    "eureetloir": "28",
    "loiret": "45",
    "yonne": "89",
    "maineetloire": "49",
    "indreetloire": "37",
    "indre": "36",
    "cher": "18",
    "nievre": "58",
    "loiretcher": "41",
    "mayenne": "53",
}

# Types à conserver (maisons / propriétés de caractère). Exclut appart./terrain.
_KEEP_TYPE = re.compile(
    r"house|farmhouse|chateau|château|manor|manoir|longere|longère|cottage|"
    r"mill|moulin|mansion|property|estate|villa|barn|presbyt|domaine|"
    r"countryside|village",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"apartment|appartement|studio|building plot|\bplot\b|\bland\b|garage|"
    r"parking|commercial|business|\bshop\b|\boffice\b|hotel|gite complex",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0) or 0
    prix_min = criteres.get("prix_min", 0) or 0
    surface_min = criteres.get("surface_min", 0) or 0

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
                print(f"[MyFrenchHouse] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[MyFrenchHouse] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.7)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    slug: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    url = f"{BASE_URL}/property-for-sale-in-france/in/{slug}"
    r = await client.get(url)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    cards = soup.select("div.group.flex.flex-col")

    biens: list[dict] = []
    seen: set[str] = set()

    for card in cards:
        try:
            bien = _parse_card(card)
        except Exception:
            continue
        if not bien:
            continue

        # POST-FILTRE département : écarte les cartes « featured » hors-zone.
        if bien["departement"] != dept:
            continue

        aid = bien["id_annonce"] or bien["url"]
        if aid in seen:
            continue

        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        seen.add(aid)
        biens.append(bien)

    return biens


def _parse_card(card) -> dict | None:
    a = card.select_one('a[href*="/property-in-france/"]')
    if not a or not a.get("href"):
        return None
    url = a["href"].strip()
    if not url.startswith("http"):
        url = BASE_URL + url
    url = url.split("?")[0]

    # id_annonce : fin d'URL /{type}/{id} ; secours x-data id:NNN
    m_id = re.search(r"/(\d+)/?$", url)
    id_annonce = m_id.group(1) if m_id else None
    if not id_annonce:
        m_x = re.search(r"id:\s*(\d+)", card.get("x-data", "") or "")
        id_annonce = m_x.group(1) if m_x else None

    # Département : slug d'URL fiche = {ville}-{dept}-{region}
    dept_code = _dept_from_slug(url)

    # Référence MFH-xxx
    ref = None
    ref_el = card.find(string=re.compile(r"Ref:\s*MFH", re.IGNORECASE))
    if ref_el:
        m_ref = re.search(r"(MFH-[A-Za-z0-9]+)", ref_el)
        if m_ref:
            ref = m_ref.group(1)

    # Titre
    titre = ""
    h = card.select_one("h3 a, h2 a, h3, h2")
    if h:
        titre = (h.get("title") or h.get_text(" ", strip=True)).strip()
    titre = re.sub(r"\s+", " ", titre).strip()

    # Zone info (texte) — on travaille sur le bloc info
    info = card.select_one("div.flex.flex-1.flex-col") or card
    info_text = info.get_text("|", strip=True)

    # Type : "{Type} for Sale in"
    type_bien = "maison"
    m_type = re.search(r"\|?\s*([A-Za-zÀ-ÿ \-]+?)\s+for Sale", info_text, re.IGNORECASE)
    type_raw = m_type.group(1).strip() if m_type else ""
    if type_raw:
        type_bien = type_raw.lower()

    # Exclusion par type (appart./terrain…)
    blob = f"{type_raw} {titre} {url}"
    if _EXCLUDE_TYPE.search(blob) and not _KEEP_TYPE.search(blob):
        return None

    # Localisation : "Ville, Département"
    ville = ""
    m_loc = re.search(r"\|([^|]+?),\s*([A-Za-zÀ-ÿ' \-]+?)\|", info_text)
    if m_loc:
        ville = m_loc.group(1).strip()
        if not dept_code:
            dept_code = DEPT_NAMES.get(_norm(m_loc.group(2)), "")
    if not ville:
        # secours : ville depuis le slug (1er segment avant le dept)
        ville = _ville_from_slug(url)

    # Prix : "€1,248,000"
    prix = None
    m_price = re.search(r"€\s*([\d,\.]+)", info_text)
    if m_price:
        prix = _to_float(m_price.group(1))

    # Specs : surface "NNN m²", terrain "NN.NN ha", chambres (1er entier nu)
    surface = None
    m_surf = re.search(r"([\d,\.]+)\s*m²", info_text)
    if m_surf:
        surface = _to_float(m_surf.group(1))

    surface_terrain = None
    m_land = re.search(r"([\d,\.]+)\s*ha", info_text)
    if m_land:
        ha = _to_float(m_land.group(1))
        if ha is not None:
            surface_terrain = round(ha * 10000)

    chambres = None
    # le nombre de chambres est l'entier isolé juste avant la surface "NNN m²"
    m_beds = re.search(r"\|(\d{1,2})\|[\d,\.]+\s*m²", info_text)
    if m_beds:
        chambres = int(m_beds.group(1))

    # Photos (BunnyCDN) — déduplique par numéro d'image
    photos: list[str] = []
    seen_img: set[str] = set()
    for img in card.select("img[src]"):
        src = (img.get("src") or "").strip()
        if "/properties/" not in src:
            continue
        clean = src.split("?")[0]
        if clean in seen_img:
            continue
        seen_img.add(clean)
        photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    if not titre:
        titre = f"{type_bien.title()} {ville}".strip() or "Propriété My French House"

    return {
        "source": "my_french_house",
        "url": url,
        "id_annonce": ref or id_annonce or url,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": dept_code,
        "ville": ville[:80],
        "code_postal": "",
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "My French House",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _norm(name: str) -> str:
    """'Eure-et-Loir' → 'eureetloir' (sans accents, tirets, espaces)."""
    s = name.lower().strip()
    repl = {
        "à": "a", "â": "a", "ä": "a", "é": "e", "è": "e", "ê": "e", "ë": "e",
        "î": "i", "ï": "i", "ô": "o", "ö": "o", "û": "u", "ù": "u", "ü": "u",
        "ç": "c", "'": "", "’": "",
    }
    for k, v in repl.items():
        s = s.replace(k, v)
    return re.sub(r"[\s\-]", "", s)


def _dept_from_slug(url: str) -> str:
    """Extrait le code dept du slug fiche /property-in-france/{ville-dept-region}/.

    Cherche un nom de département connu (DEPT_NAMES) dans le slug. Les noms
    composés (eure-et-loir, indre-et-loire) sont testés avant les simples
    (indre, loir) pour éviter les faux positifs.
    """
    m = re.search(r"/property-in-france/([^/]+)/", url)
    if not m:
        return ""
    norm_slug = _norm(m.group(1))
    # Tester les noms les plus longs d'abord (composés avant simples)
    for name in sorted(DEPT_NAMES, key=len, reverse=True):
        if name in norm_slug:
            return DEPT_NAMES[name]
    return ""


def _ville_from_slug(url: str) -> str:
    m = re.search(r"/property-in-france/([^/]+)/", url)
    if not m:
        return ""
    slug = m.group(1)
    # retire le segment dept-region : on garde tout avant le nom de dept connu
    norm_slug = _norm(slug)
    for name in sorted(DEPT_NAMES, key=len, reverse=True):
        idx = norm_slug.find(name)
        if idx > 0:
            # reconstruit approximativement à partir du slug original
            parts = slug.split("-")
            # on prend les premiers tokens jusqu'au dept (heuristique)
            keep = []
            acc = ""
            for p in parts:
                acc += p
                keep.append(p)
                if name.startswith(_norm(acc)) and _norm(acc) == name:
                    keep = keep[:-len(name.split("-")) or None]
                    break
            return " ".join(keep[:3]).title()
    return slug.replace("-", " ").title()


def _to_float(text: str) -> float | None:
    cleaned = re.sub(r"[^\d.]", "", str(text).replace(",", ""))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


# ── CLI standalone ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from collections import Counter

    try:
        from config_loader import load_criteria

        criteres = load_criteria()
        depts = criteres.departements
        prix_max = criteres.prix_max
        prix_min = getattr(criteres, "prix_min", 0)
        surface_min = criteres.surface_min
    except Exception:
        depts = list(DEPT_SLUGS.keys())
        prix_max = prix_min = surface_min = 0

    biens = asyncio.run(
        search(
            {
                "departements": depts,
                "prix_max": prix_max,
                "prix_min": prix_min,
                "surface_min": surface_min,
            }
        )
    )
    print(f"\nTotal My French House: {len(biens)} annonces")
    by_dep = Counter(b["departement"] for b in biens)
    print("Par département:", dict(sorted(by_dep.items())))
    leaks = [b for b in biens if b["departement"] not in [str(d).zfill(2) for d in depts]]
    print(f"FUITES hors-zone : {len(leaks)}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

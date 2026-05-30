"""scrapers/vineatransaction.py — Vinea Transaction (propriétés VITICOLES)

Méthode : scrape_simple (httpx) — SSR WordPress.
URL listing : https://www.vineatransaction.com/fr/domaine-viticole/{slug}
  - slug région large : loire, bordeaux, provence, languedoc…  (taxonomie `domaine_region`)
  - slug département/appellation : maine-et-loire-49, loire-et-cher-41, chinon…
    (taxonomie fine, créée à la demande quand un bien y est rattaché)
Cards : article.domaine
  - URL    : a.post-thumbnail-inner[href]  (ou 1er a[href])
  - Ref    : .entry-ref  ("Ref 4071")  → id_annonce
  - Type   : .entry-type  ("Propriétés viticoles Loire")  → catégorie + région large
  - Titre/desc : .entry-infos / .entry-content  (texte libre, sous-région : "ANJOU", "SAUMUR"…)
  - Surface: en HECTARES dans le titre/slug ("17ha", "5 ha de vignes")  → m² ×10000
  - Photo  : img.wp-post-image[src|data-lazy-src]

LIMITES CRITIQUES (→ blacklist, actif:false) :
  1. AUCUN code postal ni commune nulle part (ni liste ni détail) : la localisation
     se limite à une région large (Loire) + une sous-région en texte libre
     (Anjou, Saumur, Touraine, Sancerre…). Impossible de post-filtrer par code_postal[:2].
  2. Le filtre département (slug taxonomie `-NN`) FUIT : la page `loire-et-cher-41`
     renvoie des biens taggués Touraine (37), Saumur (49) et même AOP Bordeaux (33)
     dans le conteneur de listing principal (vérifié 2026-05-30). Le tagging
     multi-taxonomie est non fiable et il n'y a pas de CP pour rattraper.
  3. Prix masqués ("Veuillez nous contacter pour connaître le prix de vente") :
     prix=None systématiquement.
  4. Stock = domaines/exploitations viticoles (vignes, chais), pas les maisons /
     manoirs / longères recherchés ; seule une poignée de "Belles demeures" /
     châteaux viticoles correspond vaguement.

Le département retourné est donc une ESTIMATION basée sur le slug + correspondance
du nom de département dans le titre (best-effort, non garanti). Garde-fou de fuite :
on n'accepte un bien que si le nom du département cible apparaît dans le texte du
titre/description, sinon on le marque incertain.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.vineatransaction.com"
MAX_PAGES = 6

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Slugs de taxonomie connus (200 + biens) par département cible.
# Seuls 49, 41, 37 ont un slug exploitable ; les autres n'existent pas (404).
DEPT_SLUGS: dict[str, list[str]] = {
    "49": ["maine-et-loire-49"],
    "41": ["loire-et-cher-41"],
    "37": ["chinon"],            # appellation 100 % Indre-et-Loire
    # 72, 28, 45, 89, 36, 18, 58, 53 : aucun slug taxonomie (404) → pas de stock exposé
}

# Noms de département / sous-région servant de garde-fou anti-fuite (dans le titre).
DEPT_HINTS: dict[str, re.Pattern] = {
    "49": re.compile(r"maine[- ]et[- ]loire|\banjou\b|\bsaumur\b|layon|savenni[eè]res", re.I),
    "41": re.compile(r"loir[- ]et[- ]cher|cheverny|cour[- ]cheverny", re.I),
    "37": re.compile(r"indre[- ]et[- ]loire|touraine|chinon|bourgueil|vouvray|montlouis|azay", re.I),
}

_EXCLUDE_TYPE = re.compile(r"\bvigne\b|\bvignes\b|terrain|local|commerce", re.I)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    surface_min = criteres.get("surface_min") or 0  # m² habitables (rarement dispo ici)

    results: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            slugs = DEPT_SLUGS.get(dept)
            if not slugs:
                print(f"[VineaTransaction] Dept {dept}: aucun slug taxonomie (pas de stock exposé)")
                continue
            n_dept = 0
            for slug in slugs:
                try:
                    biens = await _scrape_slug(client, slug, dept)
                except Exception as e:
                    print(f"[VineaTransaction] Erreur dept {dept} slug {slug}: {e}")
                    continue
                for b in biens:
                    aid = b.get("id_annonce") or b.get("url")
                    if aid in seen:
                        continue
                    # GARDE-FOU anti-fuite : le nom du département cible doit apparaître
                    # dans le titre/description (le slug taxonomie fuit hors-dept).
                    hint = DEPT_HINTS.get(dept)
                    blob = f"{b.get('titre', '')} {b.get('description', '')}"
                    if hint and not hint.search(blob):
                        continue
                    seen.add(aid)
                    results.append(b)
                    n_dept += 1
                await asyncio.sleep(0.5)
            print(f"[VineaTransaction] Dept {dept}: {n_dept} annonces (après garde-fou)")

    return results


async def _scrape_slug(client: httpx.AsyncClient, slug: str, dept: str) -> list[dict]:
    biens: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = f"{BASE_URL}/fr/domaine-viticole/{slug}"
        else:
            url = f"{BASE_URL}/fr/domaine-viticole/{slug}/page/{page}"
        r = await client.get(url)
        if r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("article.domaine")
        if not cards:
            break
        for card in cards:
            b = _parse_card(card, dept)
            if b:
                biens.append(b)
        # page suivante ?
        if not soup.select_one(f'a[href*="/{slug}/page/{page + 1}"]'):
            break
        await asyncio.sleep(0.4)
    return biens


def _parse_card(card, dept: str) -> dict | None:
    a = card.select_one("a.post-thumbnail-inner") or card.select_one("a[href]")
    if not a or not a.get("href"):
        return None
    url = a["href"].strip()
    if not url.startswith("http"):
        url = BASE_URL + url

    ref_el = card.select_one(".entry-ref")
    ref = ref_el.get_text(strip=True) if ref_el else ""
    m_ref = re.search(r"(\d+)", ref)
    id_annonce = m_ref.group(1) if m_ref else url

    type_el = card.select_one(".entry-type")
    cat = type_el.get_text(" ", strip=True) if type_el else ""

    # Titre + description : .entry-content + heading dans .entry-infos
    infos = card.select_one(".entry-infos")
    content_el = card.select_one(".entry-content")
    description = content_el.get_text(" ", strip=True) if content_el else ""
    # le titre descriptif = texte de .entry-infos sans la ref/type/"Découvrir le bien"
    raw = infos.get_text(" ", strip=True) if infos else ""
    titre = raw
    for junk in (ref, cat, "Découvrir le bien"):
        if junk:
            titre = titre.replace(junk, " ")
    titre = re.sub(r"\s+", " ", titre).strip()
    if not titre:
        titre = cat or "Propriété viticole"

    if _EXCLUDE_TYPE.search(cat) and "demeure" not in cat.lower():
        # vignes nues / terrains : on les garde quand même comme propriété viticole,
        # mais on note le type. (le filtre type final est fait par hunter/criteria)
        type_bien = "propriété viticole"
    elif "demeure" in cat.lower() or re.search(r"ch[aâ]teau|manoir|demeure", titre, re.I):
        type_bien = "demeure"
    else:
        type_bien = "propriété viticole"

    # Surface : en hectares dans titre/desc/slug → m² (×10000)
    surface_terrain = _parse_ha(titre) or _parse_ha(description) or _parse_ha(url)

    # photo
    photos = []
    img = card.select_one("img.wp-post-image, img")
    if img:
        src = img.get("data-lazy-src") or img.get("src") or ""
        if src.startswith("http") and not src.startswith("data:"):
            photos.append(src)

    return {
        "source": "vineatransaction",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,          # ESTIMÉ via slug + garde-fou titre (non garanti)
        "ville": None,                # jamais exposé
        "code_postal": "",            # jamais exposé
        "surface": None,              # habitable jamais exposée
        "surface_terrain": surface_terrain,  # surface viticole en m²
        "pieces": None,
        "chambres": None,
        "prix": None,                 # masqué ("nous contacter")
        "dpe": None,
        "photos": photos[:5],
        "agence": "Vinea Transaction",
    }


def _parse_ha(text: str) -> float | None:
    """'17ha', '5 ha de vignes', '2.30 hectares' → m² (×10000)."""
    if not text:
        return None
    m = re.search(r"(\d+[.,]?\d*)\s*(?:ha\b|hectares?)", text, re.I)
    if m:
        try:
            return round(float(m.group(1).replace(",", ".")) * 10000, 0)
        except ValueError:
            return None
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
    print(f"\nTotal Vinea Transaction (depts cibles): {len(biens)} annonces")
    by_dept: dict[str, int] = {}
    for b in biens:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print(f"Par département : {by_dept}")
    for b in biens[:12]:
        print(
            f"  [dept {b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']}"
        )

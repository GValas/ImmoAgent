"""scrapers/durand_montouche.py — Durand-Montouché (agence locale Orléans / Loiret)

Méthode : scrape_simple (httpx) — SSR HTML (pas de JS nécessaire).
Site    : https://www.dmimmo.com  (agence indépendante d'Orléans, 50 ans, Loiret 45).

URL liste ventes : /annonces-ventes/180-vente-tous.htm?p=N
  → SSR paginé (~19 pages, 10 biens/page). L'agence vend dans l'agglo orléanaise
    et le Loiret (45), plus quelques communes de Sologne en Loir-et-Cher (41) —
    les deux départements sont dans la zone cible.

Filtre département : les cartes n'exposent PAS le code postal (ni microdata, ni
  breadcrumb fiable sur la page détail — les seuls 45xxx présents sont l'adresse
  de l'agence en pied de page). Le dept est donc garanti par un **mapping
  ville → code postal** (VILLE_CP), limité aux communes du réseau (Loiret 45 +
  Sologne 41, toutes en zone cible). Un bien dont la ville ne résout PAS à un CP
  dont le préfixe est un département cible est ÉCARTÉ (post-filtre strict,
  0 fuite hors-zone).

Cartes : div.item[data-id]
  - URL   : a.positionImg[href]  → /annonce/{REF}_9/180-achat-...htm
  - Réf   : segment {REF} de l'URL détail (id_annonce)
  - Prix  : span[itemprop="price"]  →  "319 900" (devise EUR)
  - Type  : span.typeBien  →  "Maison 5 Pièces"  (type + nb pièces)
  - Ville : span.ville  →  "OLIVET" (majuscules)
  - Surf  : span.superficie span  →  "114 m²"
  - Desc  : p.noCarte
  - Photo : span.imgAcc img[src]

Type de bien : on ne garde que maison / propriété / longère / manoir / domaine
  (exclusion appartement, local, terrain, immeuble, parking).

Couverture : agglo d'Orléans uniquement (Loiret). Inventaire de vente modeste
  (~10 biens tous types), peu de maisons ≥ 150 m². Scraper fonctionnel et
  leak-proof ; à réactiver si le stock zone grossit.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import unicodedata

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.dmimmo.com"
LIST_URL = f"{BASE_URL}/annonces-ventes/180-vente-tous.htm"
MAX_PAGES = 22
PHOTOS_PER_CARD = 1  # la liste n'expose qu'une vignette ; gallery.py enrichira

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Mapping ville (clé normalisée sans accents, minuscules) → code postal Loiret.
# Couvre l'agglo d'Orléans + communes courantes du Loiret. Toute ville absente
# de ce map est ÉCARTÉE → garantit 0 fuite hors-département.
VILLE_CP: dict[str, str] = {
    "orleans": "45000",
    "olivet": "45160",
    "fleury les aubrais": "45400",
    "saint jean de braye": "45800",
    "st jean de braye": "45800",
    "saint jean le blanc": "45650",
    "st jean le blanc": "45650",
    "saint jean de la ruelle": "45140",
    "st jean de la ruelle": "45140",
    "saint pryve saint mesmin": "45750",
    "st pryve st mesmin": "45750",
    "la chapelle saint mesmin": "45380",
    "la chapelle st mesmin": "45380",
    "saran": "45770",
    "ingre": "45140",
    "ormes": "45140",
    "checy": "45430",
    "mardie": "45430",
    "combleux": "45800",
    "semoy": "45400",
    "boigny sur bionne": "45760",
    "marigny les usages": "45760",
    "chanteau": "45400",
    "saint denis en val": "45560",
    "st denis en val": "45560",
    "sandillon": "45640",
    "jargeau": "45150",
    "darvoy": "45150",
    "vienne en val": "45510",
    "tigy": "45510",
    "neuville aux bois": "45170",
    "chateauneuf sur loire": "45110",
    "sully sur loire": "45600",
    "beaugency": "45190",
    "meung sur loire": "45130",
    "clery saint andre": "45370",
    "clery st andre": "45370",
    "la ferte saint aubin": "45240",
    "la ferte st aubin": "45240",
    "ardon": "45160",
    "pithiviers": "45300",
    "montargis": "45200",
    "amilly": "45200",
    "gien": "45500",
    "briare": "45250",
    "chalette sur loing": "45120",
    "courtenay": "45320",
    "ferrieres en gatinais": "45210",
    "malesherbes": "45330",
    "bellegarde": "45270",
    "patay": "45310",
    "artenay": "45410",
    "chevilly": "45520",
    "trainou": "45470",
    "fay aux loges": "45450",
    "vitry aux loges": "45530",
    "lailly en val": "45740",
    "dry": "45370",
    "jouy le potier": "45370",
    "ligny le ribault": "45240",
    "huisseau sur mauves": "45130",
    "baccon": "45130",
    "villorceau": "45190",
    "feroles": "45150",
    "ferolles": "45150",
    "saint cyr en val": "45590",
    "st cyr en val": "45590",
    "saint denis de l hotel": "45550",
    "st denis de l hotel": "45550",
    "vennecy": "45760",
    # Sologne — Loir-et-Cher (41), zone cible
    "nouan le fuzelier": "41600",
    "vouzon": "41600",
}

_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|maison de village",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"boutique|entrep[oô]t",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    """Minuscule, sans accents, tirets/apostrophes → espaces, espaces réduits."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("'", " ").replace("-", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Réseau implanté en Loiret (45) + Sologne (41). Si aucun n'est demandé, rien.
    if not ({"45", "41"} & departements):
        print("[DurandMontouche] Aucun dept couvert (45/41) demandé → 0 annonce")
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{LIST_URL}?p={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[DurandMontouche] Erreur réseau page {page} : {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.item")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # Post-filtre dept STRICT : la ville doit résoudre à un CP dont le
                # préfixe est un département cible (0 fuite hors-zone).
                cp = bien["code_postal"]
                if not cp or cp[:2] not in departements:
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

            await asyncio.sleep(0.5)

    print(f"[DurandMontouche] Total : {len(results)} annonces retenues (zone cible)")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a.positionImg") or card.select_one(
        "a[href*='/annonce/']"
    )
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Réf depuis /annonce/{REF}_9/...
    m_ref = re.search(r"/annonce/([^/]+?)_\d+/", href)
    id_annonce = m_ref.group(1) if m_ref else url

    # Type de bien + nb pièces depuis span.typeBien ("Maison 5 Pièces")
    type_el = card.select_one(".typeBien")
    type_txt = type_el.get_text(" ", strip=True) if type_el else ""
    if _EXCLUDE_TYPE.search(type_txt) and not _KEEP_TYPE.search(type_txt):
        return None
    if not _KEEP_TYPE.search(type_txt):
        return None
    type_bien = re.sub(r"\d+\s*pi[eè]ces?", "", type_txt, flags=re.IGNORECASE)
    type_bien = type_bien.strip() or "maison"
    pieces = None
    m_p = re.search(r"(\d+)\s*pi[eè]ces?", type_txt, re.IGNORECASE)
    if m_p:
        pieces = int(m_p.group(1))

    # Ville + résolution code postal Loiret
    ville_el = card.select_one(".ville")
    ville_raw = ville_el.get_text(" ", strip=True) if ville_el else ""
    ville = ville_raw.title() if ville_raw else ""
    code_postal = VILLE_CP.get(_norm(ville_raw), "")
    departement = code_postal[:2] if code_postal else ""

    # Prix
    price_el = card.select_one("[itemprop='price']")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Surface
    surf_el = card.select_one(".superficie")
    surface = _parse_surface(surf_el.get_text(" ", strip=True) if surf_el else "")

    # Description
    desc_el = card.select_one("p.noCarte")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Titre : alt de l'image ou type + ville
    img = card.select_one("span.imgAcc img")
    titre = ""
    if img:
        titre = (img.get("alt") or "").strip()
    if not titre:
        titre = f"{type_bien} {ville}".strip()

    # Photo (vignette)
    photos: list[str] = []
    if img:
        src = img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    # Terrain : tenté depuis la description (souvent "terrain de NNN m²")
    surface_terrain = _parse_terrain(description)

    return {
        "source": "durand_montouche",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien.lower(),
        "description": description[:1200],
        "departement": departement,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Durand-Montouché",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    m = re.search(r"(\d[\d\s\xa0]*)\s*m", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 3000:
                return f
        except ValueError:
            pass
    return None


def _parse_terrain(text: str) -> float | None:
    if not text:
        return None
    m = re.search(
        r"terrain[^0-9]{0,20}?([\d\s\xa0]{2,})\s*m", text, re.IGNORECASE
    )
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 50 <= f <= 200000:
                return f
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
    print(f"\nTotal Durand-Montouché : {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

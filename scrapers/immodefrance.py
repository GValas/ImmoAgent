"""scrapers/immodefrance.py — IMMO de France (réseau Procivis)

Méthode : scrape_simple (httpx) — SSR HTML (Rails + Tailwind).

Domaine & redirection :
    immodefrance.com redirige (301) vers procivis.fr — le listing national vit sur
    https://www.procivis.fr/acheter  (réseau IMMO de France / Procivis).

Filtre département : CÔTÉ SERVEUR via la querystring `locations[]=DP{NN}`
    (token « Département » ; ex DP53 = Mayenne). Vérifié : aucune fuite hors-dept.
    Le token est obtenu via l'endpoint d'autocomplétion `/locations/autocomplete?search=...`
    mais comme il est déterministe (`DP` + code dept), on le construit directement.
    market_type=transac (achat), property_types[]=house (maisons uniquement).

Listing :
    GET /acheter?market_type=transac&locations[]=DP{NN}&property_types[]=house&page=N
    Cartes : li[data-pagination-target=item] (16 / page, pagy)
      - URL/type : a[href] → /acheter/maisons/{region}/{dept-slug}/{ville}/{id}
                   (segment 1 du path = type : maisons / appartements / terrains…)
      - Texte agrégé : "Maison Saint-Berthevin (53940) 68,48 m² 3 p. DPE : B 199 000 €"
        → ville, code_postal, surface, pièces, DPE, prix extraits par regex.
      - Photo : img[src] (URL relative /medias/...)

Couverture : réseau à implantation inégale. Sur les départements cibles, stock réel
    en 28, 49, 53 ; 0 bien en 72/45/89/37/36/18/58/41 (au 2026-05-30). 0 fuite.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.procivis.fr"
LISTING_URL = f"{BASE_URL}/acheter"
MAX_PAGES = 15
PHOTOS_PER_CARD = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Segment d'URL (type) → label / conservation. On ne garde que maisons & assimilés.
_TYPE_FROM_SLUG = {
    "maisons": "maison",
    "maison": "maison",
    "appartements": "appartement",
    "terrains": "terrain",
    "immeubles": "immeuble",
    "locaux-commerciaux": "local commercial",
    "bureaux": "bureau",
    "parkings": "parking",
    "autres": "autre",
}
_KEEP_TYPES = {"maison"}

# Préfixe carrousel parfois présent dans le texte des cartes
_CAROUSEL_PREFIX = re.compile(r"^(?:Slide pr[ée]c[ée]dente\s*)?(?:Slide suivante\s*)?", re.IGNORECASE)
_DPE_RE = re.compile(r"DPE\s*:\s*([A-G])", re.IGNORECASE)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            try:
                biens = await _scrape_dept(
                    client, dept, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[ImmoDeFrance] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[ImmoDeFrance] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.5)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        params = [
            ("market_type", "transac"),
            ("locations[]", f"DP{dept}"),
            ("property_types[]", "house"),
            ("page", str(page)),
        ]
        r = await client.get(LISTING_URL, params=params)
        if r.status_code != 200:
            break

        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("li[data-pagination-target=item]")
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            try:
                bien = _parse_card(card, dept)
            except Exception:
                continue
            if not bien:
                continue

            # Sécurité anti-fuite : on n'accepte que le département cible
            cp = bien.get("code_postal") or ""
            if cp and cp[:2] != dept:
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
            biens.append(bien)
            new_on_page += 1

        if new_on_page == 0:
            break

        # Dernière page : pas de lien vers page+1
        if not soup.select_one(f'a[href*="page={page + 1}"]'):
            break

        await asyncio.sleep(0.4)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    a = card.select_one("a[href]")
    href = a.get("href", "") if a else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type depuis le 1er segment du path : /acheter/{type-slug}/...
    parts = [p for p in href.split("/") if p]
    type_slug = parts[1] if len(parts) > 1 else ""
    type_bien = _TYPE_FROM_SLUG.get(type_slug, type_slug.replace("-", " "))
    if type_bien not in _KEEP_TYPES:
        return None

    # id annonce = dernier segment du path
    id_annonce = parts[-1] if parts else url

    # Texte agrégé de la carte
    raw = card.get_text(" ", strip=True)
    raw = _CAROUSEL_PREFIX.sub("", raw).strip()
    raw = re.sub(r"\s*Acc[ée]der [àa] l['’]annonce\s*$", "", raw, flags=re.IGNORECASE).strip()

    # Localisation : "... Ville (CODEPOSTAL) ..."
    ville, code_postal = "", ""
    m_loc = re.search(r"([A-Za-zÀ-ÿ' \-]+?)\s*\((\d{5})\)", raw)
    if m_loc:
        ville = m_loc.group(1).strip()
        code_postal = m_loc.group(2)
        # retire le préfixe type ("Maison ") collé à la ville
        ville = re.sub(r"^(?:Maison|Appartement|Villa|Propri[ée]t[ée]|Terrain)\s+", "", ville, flags=re.IGNORECASE).strip()

    # Surface : "68,48 m²"
    surface = None
    m_surf = re.search(r"([\d\s\xa0]+(?:[.,]\d+)?)\s*m²", raw)
    if m_surf:
        val = m_surf.group(1).replace("\xa0", "").replace(" ", "").replace(",", ".")
        try:
            f = float(val)
            if 5 <= f <= 5000:
                surface = f
        except ValueError:
            pass

    # Pièces : "3 p."
    pieces = None
    m_p = re.search(r"(\d+)\s*p\.", raw)
    if m_p:
        pieces = int(m_p.group(1))

    # DPE
    dpe = None
    m_dpe = _DPE_RE.search(raw)
    if m_dpe:
        dpe = m_dpe.group(1).upper()

    # Prix : dernier "NNN NNN €"
    prix = None
    prices = re.findall(r"([\d][\d\s\xa0]*)\s*€", raw)
    if prices:
        val = prices[-1].replace("\xa0", "").replace(" ", "")
        try:
            prix = float(val)
        except ValueError:
            prix = None

    # Titre lisible
    titre = f"Maison {ville} ({code_postal})".strip() if ville else (raw[:80] or "Maison")

    # Photo
    photos = []
    img = card.select_one("img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src.startswith("/"):
            src = BASE_URL + src
        if src.startswith("http") and not src.startswith("data:"):
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "immodefrance",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": None,
        "departement": code_postal[:2] if code_postal else dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "dpe": dpe,
        "photos": photos,
        "agence": "IMMO de France (Procivis)",
    }


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
    print(f"\nTotal IMMO de France: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    leaks = [b for b in biens if b["code_postal"] and b["code_postal"][:2] not in
             [str(d).zfill(2) for d in criteres.departements]]
    print(f"FUITES hors-dept : {len(leaks)}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — DPE {b.get('dpe') or '?'}"
            f" — {b['type_bien']} — {b['ville']}"
        )

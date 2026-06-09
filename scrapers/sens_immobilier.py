"""scrapers/sens_immobilier.py — Sens Immobilier (agence indépendante, Yonne 89)

Agence indépendante basée à Sens (89100) couvrant le nord de l'Yonne (Sens,
Joigny, Migennes, Saint-Julien-du-Sault…). Site SSR (thème WordPress « H2I ») :
les annonces sont dans le HTML brut → httpx pur.

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /nos-annonces?type_recherche=VE&numero_page=N   (N = 1..MAX_PAGES)
              → AUCUN filtre département côté serveur (agence mono-secteur Yonne),
                mais le code postal est présent dans l'URL détail ET dans .secteur.
                Filtre dept = POST-FILTRE STRICT sur ce CP.

Pagination : pas de pager numéroté dans le HTML, mais ?numero_page=N fonctionne et
             renvoie des annonces distinctes ; au-delà de la dernière page, le site
             reboucle (ré-affiche d'anciennes annonces). On s'arrête donc dès qu'une
             page n'apporte aucun id inédit (dédoublonnage par id).

Filtre département (0 fuite) :
  - lien détail : /nos-annonces/ventes/{CP}-{ville}/{type}/{id}/  → CP en clair ;
  - redondance : .secteur → "Secteur 89100 SENS".
  On retient le CP du lien (recoupé avec .secteur) et on n'accepte la carte que si
  CP[:2] ∈ départements cibles.

Cartes : .offre-item
  - URL    : a[href*="/nos-annonces/ventes/"]
  - Prix/surf : .prix-surface  → "66 m² 108 000.00 € Honoraires inclus"
  - Loc    : .secteur  → "Secteur 89100 SENS"
  - Type   : .details  → "Maison T5 et +" / "Appartement T3"
  - Photo  : style background-image de .offre-item

Type de bien : on ne garde que maisons / propriétés / fermes…,
               on exclut appartements / terrains / locaux / immeubles.

Couverture : Yonne (89) (les autres départements cibles → 0 bien, normal).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.sens-immobilier.fr"
LIST_PATH = "/nos-annonces?type_recherche=VE&numero_page={page}"
MAX_PAGES = 25
PHOTOS_PER_CARD = 1  # 1 visuel exposé en liste (background-image)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_KEEP_TYPE = re.compile(
    r"maison|villa|propri[eé]t[eé]|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|mas|g[iî]te|corps de ferme|maison de village|pavillon|"
    r"bourg|campagne|b[aâ]tisse",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|viager|hangar|studio|loft|grange",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}{LIST_PATH.format(page=page)}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[SensImmo] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(".offre-item")
            if not cards:
                break

            new_ids = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                new_ids += 1

                # POST-FILTRE STRICT — 0 fuite hors-zone
                cp = bien["code_postal"]
                if not cp or cp[:2] not in departements:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                results.append(bien)

            # Plus aucun id inédit sur cette page → fin réelle (le site reboucle)
            if new_ids == 0:
                break

            await asyncio.sleep(0.5)

    print(f"[SensImmo] {len(results)} annonces (depts {sorted({b['departement'] for b in results}) or '∅'})")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a[href*='/nos-annonces/ventes/']")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

    # /nos-annonces/ventes/{CP}-{ville}/{type}/{id}/
    m = re.search(r"/ventes/(\d{5})-([^/]+)/([a-zA-Z\-]+)/(\d+)", href)
    if not m:
        return None
    code_postal = m.group(1)
    ville_slug = m.group(2)
    type_slug = m.group(3)
    id_annonce = m.group(4)

    # Type de bien (lib carte + slug URL)
    details_el = card.select_one(".details")
    type_text = details_el.get_text(" ", strip=True) if details_el else type_slug
    blob = f"{type_text} {type_slug}"
    if _EXCLUDE_TYPE.search(blob) and not _KEEP_TYPE.search(blob):
        return None
    if not _KEEP_TYPE.search(blob):
        return None
    type_bien = _deduce_type(blob) or "maison"

    # Secteur (ville + CP de redondance)
    secteur_el = card.select_one(".secteur")
    secteur = secteur_el.get_text(" ", strip=True) if secteur_el else ""
    ville = _ville_from_secteur(secteur) or ville_slug.replace("-", " ").title()

    # Prix / surface
    ps_el = card.select_one(".prix-surface")
    ps = ps_el.get_text(" ", strip=True) if ps_el else ""
    surface = _parse_surface(ps)
    prix = _parse_price(ps)

    # Pièces : "T5 et +", "T3"
    pieces = None
    m_t = re.search(r"\bT\s*(\d+)", type_text)
    if m_t:
        pieces = int(m_t.group(1))

    # Photo : background-image
    photos = []
    style = card.get("style", "") or ""
    m_img = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", style)
    if m_img:
        src = m_img.group(1)
        if src.startswith("//"):
            src = "https:" + src
        photos.append(src)

    return {
        "source": "sens_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": (f"{type_bien.title()} {ville}").strip()[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Sens Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _deduce_type(text: str) -> str:
    m = _KEEP_TYPE.search(text or "")
    return m.group(0).lower() if m else ""


def _ville_from_secteur(text: str) -> str:
    """'Secteur 89100 SENS' → 'Sens'"""
    t = re.sub(r"^\s*secteur\s*", "", text, flags=re.IGNORECASE)
    t = re.sub(r"\b\d{5}\b", "", t).strip()
    t = re.sub(r"\s+", " ", t)
    return t.title() if t else ""


def _parse_surface(text: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", text)
    if m:
        try:
            f = float(m.group(1).replace(",", "."))
            if 8 <= f <= 5000:
                return f
        except ValueError:
            pass
    return None


def _parse_price(text: str) -> float | None:
    # "66 m² 108 000.00 €" : on prend le nombre suivi de €
    m = re.search(r"([\d\s\xa0]+(?:[.,]\d{2})?)\s*€", text)
    if not m:
        return None
    raw = m.group(1)
    raw = re.sub(r"[\s\xa0]", "", raw)
    raw = re.sub(r"[.,]\d{2}$", "", raw)  # retire les centimes
    raw = re.sub(r"[^\d]", "", raw)
    try:
        v = float(raw) if raw else None
    except ValueError:
        return None
    if v and v < 1000:
        return None
    return v


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
    print(f"\nTotal Sens Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

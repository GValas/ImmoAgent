"""scrapers/cotefranceimmo.py — Côté France Immobilier (réseau de mandataires franco-belge)

Méthode : scrape_simple (httpx) — SSR HTML (CMS "Zephyr"/Apimo, rendu serveur).
URL pattern : /vente/{NN-dept-slug}/{page}   (ex: /vente/89-yonne/1)
              → filtre département CÔTÉ SERVEUR (vérifié : page "0 annonce" sur un
                dept sans stock, biens du seul dept demandé sinon). Post-filtre
                strict CP[:2] en plus, par sécurité (0 fuite).

Cartes : article.property-listing-v2__item
  - Ville : .title__content-1
  - CP    : .title__content-2   →  "(89000)"
  - Compo : .property-listing-v2__item-compo > span  →  "7 pièces - 117,12 m²"
  - Titre : h2 a.property-listing-v2__item-text  (href = URL détail)
  - Prix  : .property-listing-v2__price-value  →  "149 000 €"
  - Réf   : .property-listing-v2__item-reference > span  →  "Ref : AUX2"

Type de bien : déduit du segment d'URL détail (/{...}/N-maison/tN/...). On ne garde
               que maisons / propriétés (exclut appartement / terrain / commerce...).

Photos : non disponibles dans le HTML de la liste (chargées en JS obfusqué) → [].

Couverture : réseau concentré dans le Nord / Ardennes / frontière belge ;
             sur les départements cibles l'inventaire est faible mais réel
             (89 et 72 ont des biens ; les autres souvent 0). dernier_test 2026-06-09.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.cotefranceimmo.fr"
MAX_PAGES = 8
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Code département → slug URL cotefranceimmo.fr/vente/{NN-slug}/{page}
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

# Types de bien (segment d'URL détail) à conserver : maisons / propriétés...
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|maison-de-village|"
    r"pavillon|grange",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"boutique|hangar|entrepot|entrepôt",
    re.IGNORECASE,
)


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
                print(f"[CoteFrance] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[CoteFrance] Erreur dept {dept}: {e}")
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
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/vente/{dept}-{slug}/{page}"
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select(
            "article.property-listing-v2__item"
        )
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

            # Post-filtre dept STRICT (le filtre serveur semble bon, on re-vérifie)
            if bien["code_postal"] and bien["code_postal"][:2] != dept:
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

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("h2 a.property-listing-v2__item-text") or card.select_one(
        "h2 a"
    )
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis l'URL détail : /vente/89-yonne/18915-auxerre/1-maison/t7/.../
    type_bien = _type_from_url(href)
    if type_bien is None:
        return None

    # Localisation
    ville_el = card.select_one(".title__content-1")
    ville = ville_el.get_text(" ", strip=True) if ville_el else ""
    cp_el = card.select_one(".title__content-2")
    code_postal = ""
    if cp_el:
        m = re.search(r"(\d{5})", cp_el.get_text())
        if m:
            code_postal = m.group(1)

    # Titre
    titre = link.get_text(" ", strip=True) if link else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Référence (id_annonce)
    ref_el = card.select_one(".property-listing-v2__item-reference")
    ref_txt = ref_el.get_text(" ", strip=True) if ref_el else ""
    m_ref = re.search(r"Ref\s*:?\s*([\w-]+)", ref_txt, re.IGNORECASE)
    ref = m_ref.group(1) if m_ref else ""
    id_annonce = ref or _id_from_url(href) or url

    # Compo : "7 pièces - 117,12 m²"
    compo_el = card.select_one(".property-listing-v2__item-compo")
    compo = compo_el.get_text(" ", strip=True) if compo_el else ""
    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", compo)
    surface = _parse_surface(compo)

    # Pièces en secours : segment tN de l'URL
    if pieces is None:
        m = re.search(r"/t(\d+)/", href)
        if m:
            pieces = int(m.group(1))

    # Prix
    price_el = card.select_one(".property-listing-v2__price-value")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    return {
        "source": "cotefranceimmo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": [],
        "dpe": None,
        "agence": "Côté France Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _type_from_url(href: str) -> str | None:
    """Extrait le type depuis le segment '/N-maison/' de l'URL détail.

    Retourne None si type exclu (appartement/terrain...) ou non reconnu.
    """
    # segments du type "1-maison", "22-propriete", "2-appartement"...
    for m in re.finditer(r"/\d+-([a-zàâäéèêëîïôöùûüç-]+)/", href, re.IGNORECASE):
        seg = m.group(1)
        if _EXCLUDE_TYPE.search(seg):
            return None
        if _KEEP_TYPE.search(seg):
            return seg.replace("-", " ").strip()
    return None


def _id_from_url(href: str) -> str:
    """Récupère l'id numérique du dernier segment slug, ex '.../8784-auxerre-.../'."""
    parts = [p for p in href.split("/") if p]
    for seg in reversed(parts):
        m = re.match(r"^(\d+)-", seg)
        if m:
            return m.group(1)
    return ""


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_surface(text: str) -> float | None:
    """'7 pièces - 117,12 m²' → 117.12"""
    m = re.search(r"([\d\s\xa0]+(?:[.,]\d+)?)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
        try:
            f = float(val)
            if 8 <= f <= 5000:
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
    print(f"\nTotal Côté France: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

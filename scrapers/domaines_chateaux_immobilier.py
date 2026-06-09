"""scrapers/domaines_chateaux_immobilier.py — Domaines & Châteaux Immobilier (Tours)

Méthode : scrape_simple (httpx) — SSR HTML
Site : agence de prestige basée à Tours (vieilles pierres, demeures et propriétés
       de caractère). Couverture réelle : Indre-et-Loire (37) et Loir-et-Cher (41).

URL pattern (listings paginés, PAS de slug département) :
  /vente/{type}/{N}      ex: /vente/maison/1, /vente/propriete/1
  /demeures-a-vendre/{N} (sous-ensemble curaté ; on scrape plutôt les listings /vente/{type})
→ aucun filtre département côté serveur. On scrape le national de l'agence puis
  POST-FILTRE strict sur code_postal[:2] == dept (objectif : 0 fuite hors-zone).

Cartes : article.property-listing-v2__item
  - Ville : .title__content-1                         → "Louestault"
  - CP    : .title__content-2                         → "(37370)"
  - Compo : .property-listing-v2__item-compo          → "5 pièces - 147 m²"
  - Titre+URL : h2 a.property-listing-v2__item-text[href]
                → /vente/{id}-{ville}/{type}/{tN}/{idbien-slug}/
  - Prix  : .property-listing-v2__price-value         → "540 000 €"
  - Réf   : .property-listing-v2__item-reference      → "Ref : 7828adn"
  - Photo : img.item__img[data-src]                   → "//dc-immo.staticlbi.com/..."

Type de bien : déduit du segment d'URL (maison / propriete / villa / demeure...).
On ne garde que maisons / propriétés / demeures (pas appartement / terrain).

Couverture : inventaire faible mais réel, intégralement en 37 et 41. Sur les
départements du Val-de-Loire / Ouest hors 37-41 (72, 28, 45, 89, 49, 36, 18,
58, 53), 0 stock attendu — le scraper reste fonctionnel.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.domaines-chateaux-immobilier.com"
MAX_PAGES = 8
PHOTOS_PER_CARD = 10

# Listings de type (le site n'a pas de filtre département ; on agrège ces listes
# puis on post-filtre par CP). On reste sur les types « de caractère ».
TYPE_LISTINGS = ["maison", "propriete", "demeure-de-caractere", "villa"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien (segment d'URL) à conserver : maisons / propriétés / demeures...
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gentilhommiere|gentilhommière",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Le site ne couvre que 37 et 41 : si aucun de ces depts n'est demandé,
    # on évite des requêtes inutiles (mais on tente quand même par robustesse).
    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for type_slug in TYPE_LISTINGS:
            try:
                biens = await _scrape_listing(
                    client,
                    type_slug,
                    departements,
                    prix_max,
                    prix_min,
                    surface_min,
                    seen_ids,
                )
                results.extend(biens)
                if biens:
                    print(
                        f"[DomainesChateaux] Listing '{type_slug}': "
                        f"{len(biens)} annonces retenues (zone)"
                    )
            except Exception as e:
                print(f"[DomainesChateaux] Erreur listing '{type_slug}': {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_listing(
    client: httpx.AsyncClient,
    type_slug: str,
    departements: list[str],
    prix_max: int,
    prix_min: int,
    surface_min: int,
    seen_ids: set[str],
) -> list[dict]:
    biens: list[dict] = []

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/vente/{type_slug}/{page}"
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
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue

            cp = bien["code_postal"]
            # POST-FILTRE DÉPARTEMENT STRICT — 0 fuite hors-zone
            if not cp or cp[:2] not in departements:
                continue
            bien["departement"] = cp[:2]

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

        # Listing court : si la page n'apporte rien de neuf, on arrête.
        if new_on_page == 0 and len(cards) < 12:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card) -> dict | None:
    link = card.select_one("a.property-listing-v2__item-text")
    if link is None:
        # secours : lien obfusqué data-url
        obf = card.select_one(".js-obfuscation[data-url]")
        href = obf.get("data-url", "") if obf else ""
    else:
        href = link.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type depuis le segment d'URL : /vente/{id}-{ville}/{type}/{tN}/{idbien-slug}/
    parts = [p for p in href.split("/") if p]
    type_seg = parts[2] if len(parts) > 2 else ""
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg.replace("-", " ").strip() or "propriete"

    # id_annonce : idbien dans le bouton favori, sinon réf, sinon id du slug
    id_annonce = _extract_idbien(card)
    if not id_annonce:
        ref_el = card.select_one(".property-listing-v2__item-reference")
        ref = ref_el.get_text(" ", strip=True) if ref_el else ""
        ref = re.sub(r"^Ref\s*:\s*", "", ref, flags=re.IGNORECASE).strip()
        m = re.search(r"(\d+)-[a-z]", parts[-1]) if parts else None
        id_annonce = ref or (m.group(1) if m else "") or url

    # Localisation
    ville_el = card.select_one(".title__content-1")
    ville = ville_el.get_text(" ", strip=True) if ville_el else ""
    cp_el = card.select_one(".title__content-2")
    code_postal = ""
    if cp_el:
        m_cp = re.search(r"(\d{5})", cp_el.get_text())
        if m_cp:
            code_postal = m_cp.group(1)

    # Titre
    title_span = link.select_one("span") if link else None
    titre = (
        title_span.get_text(" ", strip=True)
        if title_span
        else (link.get_text(" ", strip=True) if link else "")
    )
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Compo : "5 pièces - 147 m²"
    compo_el = card.select_one(".property-listing-v2__item-compo")
    compo = compo_el.get_text(" ", strip=True) if compo_el else ""
    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", compo)
    surface = _parse_surface(compo)

    # Prix
    price_el = card.select_one(".property-listing-v2__price-value")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Photos
    photos = []
    for img in card.select("img.item__img, img.js-lazy"):
        src = img.get("data-src") or img.get("data-original") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "domaines_chateaux_immobilier",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Domaines & Châteaux Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_idbien(card) -> str:
    btn = card.select_one("[data-add-url]")
    if btn:
        m = re.search(r"idbien=(\d+)", btn.get("data-add-url", ""))
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
    """'5 pièces - 147 m²' → 147.0"""
    m = re.search(r"([\d\s\xa0]+)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
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
    print(f"\nTotal Domaines & Châteaux Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

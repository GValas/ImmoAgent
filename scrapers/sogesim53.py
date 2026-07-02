"""scrapers/sogesim53.py — Sogesim (agence locale Laval / Mayenne 53)

Méthode : scrape_simple (httpx) — SSR HTML (CMS immo générique type BCV/Blois).
URL pattern : /annonces/transaction/vente.html?manufacturers_id=transaction&page=N&search_id=&sort=0
              → listing NATIONAL de l'agence (mono-département de fait : 100 % 53).
              Pas de filtre serveur par département → POST-FILTRE strict code_postal[:2].

Cartes : div.item-product-listing
  - URL     : a[href*="/fiches/"]  → ../fiches/{cat}_{id}/{slug}.html
  - id      : le segment numérique après "_" dans le chemin /fiches/..._{ID}/
  - Titre   : .products-name   ("Maison Laval 4 pièce(s) 70.18 m2")
  - Prix    : .products-price  (1er nœud texte, avant span honoraires)
  - Loc     : .products-localisation  ("53000 LAVAL")
  - Desc    : .products-description  (contient "Réf : ...")
  - Photo   : img.photo-listing[src]

Type de bien : déduit du titre + préfixe catégorie du chemin /fiches/{cat}_...
               (3-33 = appartement, 4-40 = maison, 8 = immeuble). On ne garde
               que maisons / propriétés (pas appartement/immeuble/terrain).

Couverture : agence mono-département implantée sur la Mayenne (53) uniquement
             (Laval, Mayenne, Ahuillé, Cossé-le-Vivien...). ~18 biens en vente.
             Hors de la zone de test 72/28/45/89 → 0 résultat attendu sur ces
             départements (scraper fonctionnel, réactiver si 53 entre en zone).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int

BASE_URL = "https://www.sogesim53.com"
LISTING = BASE_URL + "/annonces/transaction/vente.html"
MAX_PAGES = 6
PHOTOS_PER_CARD = 10


# Types à conserver (maisons / propriétés) — exclut appartement, immeuble, terrain...
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|long[èe]re|manoir|ch[âa]teau|moulin|"
    r"demeure|domaine|mas|g[îi]te|corps[- ]de[- ]ferme|pavillon",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|studio",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = (
                f"{LISTING}?manufacturers_id=transaction"
                f"&page={page}&search_id=&sort=0"
            )
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Sogesim53] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                "div.item-product-listing"
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

                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                new_on_page += 1

                # Post-filtre département STRICT (0 fuite hors-zone)
                cp = bien.get("code_postal") or ""
                if not cp or cp[:2] not in departements:
                    continue
                bien["departement"] = cp[:2]

                # Bornes prix / surface (sans exclure un champ manquant)
                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                results.append(bien)

            # Le listing reboucle (page N>réel renvoie la dernière) → on s'arrête
            if new_on_page == 0:
                break
            await asyncio.sleep(0.5)

    # Log par département demandé
    for dept in departements:
        n = sum(1 for b in results if b["departement"] == dept)
        print(f"[Sogesim53] Dept {dept}: {n} annonces")

    return results


def _parse_card(card) -> dict | None:
    link = card.select_one('a[href*="/fiches/"]')
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = re.sub(r"^\.\./", BASE_URL + "/", href)
    if not url.startswith("http"):
        url = BASE_URL + "/" + url.lstrip("/")

    # id et préfixe catégorie : /fiches/{cat}_{id}/{slug}.html
    m_id = re.search(r"/fiches/([^/]*?)_(\d+)/", href)
    cat_prefix = m_id.group(1) if m_id else ""
    id_annonce = m_id.group(2) if m_id else url

    # Titre
    name_el = card.select_one(".products-name")
    titre = name_el.get_text(" ", strip=True) if name_el else ""

    # Type de bien : titre + préfixe catégorie du chemin
    type_bien, keep = _classify_type(titre, cat_prefix)
    if not keep:
        return None

    # Localisation : "53000 LAVAL"
    loc_el = card.select_one(".products-localisation")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    code_postal, ville = _parse_loc(loc)

    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Prix : 1er nœud texte de .products-price (avant span honoraires)
    price_el = card.select_one(".products-price")
    prix = None
    if price_el:
        raw = price_el.find(string=True, recursive=False)
        prix = _parse_price(raw or price_el.get_text(" ", strip=True))

    # Description (contient souvent "Réf : ...")
    desc_el = card.select_one(".products-description")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""
    ref = None
    m_ref = re.search(r"R[ée]f\s*:?\s*([A-Za-z0-9\-]+)", description)
    if m_ref:
        ref = m_ref.group(1)

    # Pièces / surface depuis le titre
    pieces = _parse_int(r"(\d+)\s*pi[èe]ce", titre)
    surface = _parse_surface(titre)
    chambres = _parse_int(r"(\d+)\s*chambre", titre + " " + description)

    # Photos
    photos = []
    for img in card.select("img.photo-listing, .img-product img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            src = re.sub(r"^\.\./", BASE_URL + "/", src)
            if src.startswith("//"):
                src = "https:" + src
            elif not src.startswith("http"):
                src = BASE_URL + "/" + src.lstrip("/")
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "sogesim53",
        "url": url,
        "id_annonce": ref or id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else None,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Sogesim",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _classify_type(titre: str, cat_prefix: str) -> tuple[str, bool]:
    """Retourne (type_bien, keep). Préfixe catégorie : 4-40=maison, 3-33=appart, 8=immeuble."""
    cat = cat_prefix.split("-")[0] if cat_prefix else ""
    # Catégorie immeuble explicite
    if cat == "8":
        return "immeuble", False
    if cat == "3":  # appartement
        return "appartement", False
    if cat == "4":  # maison → garder, sauf si titre contredit
        if _EXCLUDE_TYPE.search(titre) and not _KEEP_TYPE.search(titre):
            return titre.split()[0].lower() if titre else "autre", False
        return "maison", True
    # Pas de catégorie fiable → on se fie au titre
    if _EXCLUDE_TYPE.search(titre) and not _KEEP_TYPE.search(titre):
        return (titre.split()[0].lower() if titre else "autre"), False
    if _KEEP_TYPE.search(titre):
        m = _KEEP_TYPE.search(titre)
        return m.group(0).lower(), True
    # Type inconnu → exclusion prudente
    return "autre", False


def _parse_loc(text: str) -> tuple[str, str]:
    """'53000 LAVAL' → ('53000', 'Laval')"""
    m = re.search(r"(\d{5})", text)
    cp = m.group(1) if m else ""
    ville = re.sub(r"\d{5}", "", text).strip(" -,")
    ville = ville.title() if ville.isupper() else ville
    return cp, ville


def _parse_price(text: str) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'70.18 m2' / '84 m²' → float (surface habitable)."""
    if not text:
        return None
    m = re.search(r"([\d]+(?:[.,]\d+)?)\s*m[²2]", text)
    if m:
        try:
            f = float(m.group(1).replace(",", "."))
            if 5 <= f <= 5000:
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
    print(f"\nTotal Sogesim53: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

"""scrapers/immo_montargis.py — Immo Montargis (agence indépendante, Gâtinais/Loiret)

Méthode : scrape_simple (httpx) — SSR HTML (PrestaShop, cartes li.ajax_block_product).
URL pattern : /20-vente-maison-montargis-son-agglomeration  (catégorie « vente maison »
              la plus large de l'agence). L'agence ne couvre QUE l'agglomération
              montargoise → dept 45 exclusivement (vérifié : tous les CP observés
              commencent par 45). Post-filtre strict CP[:2] quand même. 0 fuite.

Cartes : li.ajax_block_product
  - Titre/URL : a.product-name[href]
  - Prix      : .price / .product-price  →  "129 000 €"
  - Desc      : .product-desc  (contient souvent le CP "45230" / "(45120)")
  - Icônes    : em.icon-home-outline (pièces), em.icon-resize-full (surface m²),
                em.icon-leaf (terrain m²)
  - Photo     : a.product_img_link img[src]

Code postal : extrait du .product-desc de la liste. Si absent (snippet « VENDU »,
              « SOUS COMPROMIS »...), on récupère le CP sur la page détail (httpx,
              seulement pour les biens survivant aux filtres prix/surface → peu de
              requêtes). Un bien sans CP 45 identifiable est ÉCARTÉ (0 fuite).

Type de bien : depuis le titre. Exclut location / terrain seul / murs & fonds /
               appartement / local commercial.

Couverture : Montargis et son agglomération (Loiret 45) — maisons anciennes,
             pavillons, fermettes du Gâtinais. dernier_test 2026-06-09.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.immo-montargis.fr"
# Catégorie listant le plus grand nombre de maisons à vendre de l'agence.
LISTING = "/20-vente-maison-montargis-son-agglomeration"
PHOTOS_PER_CARD = 5


_KEEP_TYPE = re.compile(
    r"maison|villa|propriete|propriété|ferme|fermette|longere|longère|manoir|"
    r"chateau|château|moulin|demeure|domaine|mas|pavillon|grange|maisonnette",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"\blocation\b|\bà louer\b|appartement|terrain (?:a |à )?b[aâ]tir|"
    r"terrain constructible|local commercial|murs et fonds|fonds de commerce|"
    r"commerce|garage|parking|immeuble|bureau",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    # L'agence est dans le 45 : si le 45 n'est pas ciblé, rien à faire.
    if "45" not in departements:
        return []

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        try:
            r = await client.get(BASE_URL + LISTING)
        except Exception as e:
            print(f"[ImmoMontargis] Erreur listing: {e}")
            return results
        if r.status_code != 200:
            print(f"[ImmoMontargis] Listing status {r.status_code}")
            return results

        cards = BeautifulSoup(r.text, "html.parser").select("li.ajax_block_product")
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

            # Filtres prix/surface AVANT la requête détail (économie de requêtes)
            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            # CP manquant → fallback page détail (peu de biens)
            if not bien["code_postal"]:
                cp = await _cp_from_detail(client, bien["url"])
                if cp:
                    bien["code_postal"] = cp
                    bien["departement"] = cp[:2]
                await asyncio.sleep(0.4)

            # Post-filtre dept STRICT (0 fuite) — exige un CP du dept cible
            if not bien["code_postal"] or bien["code_postal"][:2] not in departements:
                continue

            seen_ids.add(aid)
            results.append(bien)

    print(f"[ImmoMontargis] Dept 45: {len(results)} annonces")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a.product-name") or card.select_one("a.product_img_link")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

    name_el = card.select_one("a.product-name") or card.select_one(".s_title_block")
    titre = name_el.get_text(" ", strip=True) if name_el else ""
    if not titre:
        return None

    if _EXCLUDE_TYPE.search(titre):
        return None
    if not _KEEP_TYPE.search(titre):
        return None
    type_bien = _type_label(titre)

    desc_el = card.select_one(".product-desc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # On écarte les biens déjà vendus / sous compromis
    if re.search(r"\bVENDU\b|SOUS COMPROMIS", titre + " " + description, re.IGNORECASE):
        return None

    # Code postal depuis la desc (ex "45230" ou "(45120)")
    code_postal = ""
    m_cp = re.search(r"\b(\d{5})\b", description)
    if m_cp:
        code_postal = m_cp.group(1)

    # Ville : mot(s) en MAJUSCULES juste avant le CP (heuristique douce)
    ville = _parse_ville(description, code_postal)

    # Prix
    price_el = card.select_one(".price, .product-price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Icônes : pièces / surface / terrain
    pieces = None
    surface = None
    surface_terrain = None
    p_el = card.select_one("em.icon-home-outline")
    if p_el:
        m = re.search(r"(\d+)", p_el.get_text())
        if m:
            pieces = int(m.group(1))
    s_el = card.select_one("em.icon-resize-full")
    if s_el:
        surface = _num_m2(s_el.get_text())
    t_el = card.select_one("em.icon-leaf")
    if t_el:
        surface_terrain = _num_m2(t_el.get_text())

    # Photos
    photos = []
    for img in card.select("a.product_img_link img, .product_img_link img, img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:") and "default" in src or src.endswith((".jpg", ".jpeg", ".png", ".webp")):
            if not src.startswith("http"):
                src = f"{BASE_URL}/{src.lstrip('/')}"
            photos.append(src)
    photos = list(dict.fromkeys(photos))[:PHOTOS_PER_CARD]

    # id_annonce : id PrestaShop du début du slug
    m_id = re.search(r"/(\d+)-", href)
    id_annonce = m_id.group(1) if m_id else url

    return {
        "source": "immo_montargis",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else "45",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Immo Montargis",
    }


async def _cp_from_detail(client: httpx.AsyncClient, url: str) -> str:
    """Récupère un code postal 45xxx sur la page détail (fallback CP liste)."""
    try:
        r = await client.get(url)
    except Exception:
        return ""
    if r.status_code != 200:
        return ""
    cands = re.findall(r"\b(45\d{3})\b", r.text)
    return cands[0] if cands else ""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _type_label(text: str) -> str:
    m = _KEEP_TYPE.search(text)
    return m.group(0).lower() if m else "maison"


def _parse_ville(desc: str, cp: str) -> str:
    if cp:
        m = re.search(r"([A-ZÀ-Ý][A-ZÀ-Ý\s'\-]{2,})\s*\(?" + re.escape(cp), desc)
        if m:
            return m.group(1).strip().title()
    return ""


def _parse_price(text: str) -> float | None:
    head = text.split("€")[0] if "€" in text else text
    cleaned = re.sub(r"[^\d]", "", head)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _num_m2(text: str) -> float | None:
    m = re.search(r"([\d\s\xa0]+(?:[.,]\d+)?)\s*m", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
        try:
            f = float(val)
            return f if f > 0 else None
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
    print(f"\nTotal Immo Montargis: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b.get('pieces') or '?'}p — {b['ville']}"
        )

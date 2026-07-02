"""scrapers/sologne_conseil.py — Sologne Conseil Immobilier (agence de niche, Sologne)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress + back-office Apimo).
URL pattern : deux pages catégorie (tout le stock, sans pagination) :
                /biens-maisons-appartements/
                /biens-proprietes-de-chasse-belles-demeures/   (prestige / chasse)
              Pas de filtre dept côté serveur → on scrape les deux listings et on
              POST-FILTRE strict sur le code postal (CP[:2]). 0 fuite.

Cartes : div.blocItemBiens
  - Réf    : p.reference  →  "Référence : SCI 749"
  - Titre  : 1ʳᵉ ligne après la réf ("Propriété en Sologne", "Maison", ...)
  - Ville  : p.ville  →  "À Yvoy-le-Marron ( 41600 )"  (CP complet → dept fiable)
  - Surface: p.surface  →  "Surface du terrain : 3988 M² Surface totale bâtie : 140.0 m²"
  - Prix   : p.prix  →  "477 000 € Hai ..."  (on prend le 1ᵉʳ montant = prix Hai)
  - URL    : a.btnDetails / a[href*="/biens-immobiliers/"]
             slug : vente-{type}-{N}-pieces-{ville}-{cp}-ref-sci-NNN  (pieces + cp)
  - Photo  : img[src]  (CDN apimo)

Type de bien : depuis le titre / le slug d'URL. Exclut terrain seul / appartement /
               local / commerce.

Couverture : Sologne (Loir-et-Cher 41, Loiret 45, Cher 18) — maisons, propriétés
             d'agrément, domaines/territoires de chasse, belles demeures : profil
             idéal pour les critères (grands terrains arborés). Qq biens hors-zone
             (27...) écartés par le post-filtre. dernier_test 2026-06-09.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.sologne-conseil-immobilier.fr"
CATEGORIES = [
    "/biens-proprietes-de-chasse-belles-demeures/",
    "/biens-maisons-appartements/",
]
PHOTOS_PER_CARD = 5


_KEEP_TYPE = re.compile(
    r"maison|villa|propriete|propriété|ferme|fermette|longere|longère|manoir|"
    r"chateau|château|moulin|demeure|domaine|mas|pavillon|grange|gite|gîte|"
    r"corps de ferme|habitation",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"hangar|entrepot|entrepôt|etang seul",
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
        for cat in CATEGORIES:
            try:
                r = await client.get(BASE_URL + cat)
            except Exception as e:
                print(f"[SologneConseil] Erreur {cat}: {e}")
                continue
            if r.status_code != 200:
                continue

            cards = BeautifulSoup(r.text, "html.parser").select("div.blocItemBiens")
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # Post-filtre dept STRICT (pas de filtre serveur, listings multi-dept)
                if not bien["code_postal"] or bien["code_postal"][:2] not in departements:
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

            await asyncio.sleep(0.5)

    from collections import Counter

    for d, n in sorted(Counter(b["code_postal"][:2] for b in results).items()):
        print(f"[SologneConseil] Dept {d}: {n} annonces")

    return results


def _parse_card(card) -> dict | None:
    link = card.select_one('a[href*="/biens-immobiliers/"]') or card.select_one(
        "a.btnDetails"
    )
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Ville + code postal : "À Yvoy-le-Marron ( 41600 )"
    ville_el = card.select_one("p.ville")
    ville_txt = ville_el.get_text(" ", strip=True) if ville_el else ""
    m_cp = re.search(r"\(\s*(\d{5})\s*\)", ville_txt)
    code_postal = m_cp.group(1) if m_cp else ""
    if not code_postal:
        # secours : CP dans le slug d'URL
        m_url = re.search(r"-(\d{5})-ref", href)
        if m_url:
            code_postal = m_url.group(1)
    if not code_postal:
        return None
    ville = re.sub(r"\(\s*\d{5}\s*\)", "", ville_txt)
    ville = re.sub(r"^[ÀA]\s+", "", ville).strip()

    # Titre = texte de la div infos avant la ville ; on déduit le type
    gauche = card.select_one(".gauche") or card
    full = gauche.get_text(" ", strip=True)
    ref_el = card.select_one("p.reference")
    ref_txt = ref_el.get_text(" ", strip=True) if ref_el else ""
    # le titre est généralement entre la référence et la ville
    titre = ""
    if ref_txt and ville_txt:
        mid = full.split(ref_txt, 1)[-1]
        mid = mid.split(ville_txt, 1)[0]
        titre = re.sub(r"\s+", " ", mid).strip()
    if not titre:
        # depuis le slug d'URL
        m = re.search(r"/biens-immobiliers/vente-([a-z-]+?)-\d+-pieces", href)
        titre = m.group(1).replace("-", " ") if m else "bien"

    # Type
    type_src = titre + " " + href
    if _EXCLUDE_TYPE.search(titre) and not _KEEP_TYPE.search(titre):
        return None
    if not _KEEP_TYPE.search(type_src):
        return None
    type_bien = _type_label(type_src)

    # Pièces depuis le slug
    pieces = None
    m_p = re.search(r"-(\d+)-pieces", href)
    if m_p:
        v = int(m_p.group(1))
        pieces = v if v > 0 else None

    # Surfaces
    surf_el = card.select_one("p.surface")
    surf_txt = surf_el.get_text(" ", strip=True) if surf_el else ""
    surface = _parse_named_surface(
        surf_txt, r"Surface\s+totale\s+b[aâ]tie\s*:?\s*"
    ) or _parse_named_surface(surf_txt, r"Surface\s+habitable\s*:?\s*")
    surface_terrain = _parse_terrain(surf_txt)

    # Prix : 1ᵉʳ montant de p.prix (= prix Hai honoraires inclus)
    prix_el = card.select_one("p.prix")
    prix = _parse_price(prix_el.get_text(" ", strip=True) if prix_el else "")

    # id_annonce = ref SCI
    m_ref = re.search(r"SCI\s*(\d+)", ref_txt, re.IGNORECASE)
    if not m_ref:
        m_ref = re.search(r"ref-sci-(\d+)", href, re.IGNORECASE)
    id_annonce = ("SCI" + m_ref.group(1)) if m_ref else url

    # Photos
    photos = []
    for img in card.select("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:") and src.startswith("http"):
            photos.append(src)
    photos = list(dict.fromkeys(photos))[:PHOTOS_PER_CARD]

    return {
        "source": "sologne_conseil",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Sologne Conseil Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _type_label(text: str) -> str:
    m = _KEEP_TYPE.search(text)
    return m.group(0).lower() if m else "propriete"


def _parse_named_surface(text: str, label_pattern: str) -> float | None:
    """Extrait le nombre en m² qui suit une étiquette ('Surface du terrain : N M²')."""
    if not text:
        return None
    m = re.search(
        label_pattern + r"([\d\s\xa0]+(?:[.,]\d+)?)\s*[mM]", text, re.IGNORECASE
    )
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
        try:
            f = float(val)
            return f if f > 0 else None
        except ValueError:
            pass
    return None


def _parse_terrain(text: str) -> float | None:
    """'Surface du terrain : 3988 M²' → 3988 ; '... 7,92 Ha' → 79200 (m²)."""
    if not text:
        return None
    m = re.search(
        r"Surface\s+du\s+terrain\s*:?\s*([\d\s\xa0]+(?:[.,]\d+)?)\s*(ha|m)",
        text, re.IGNORECASE,
    )
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
    try:
        f = float(val)
    except ValueError:
        return None
    if m.group(2).lower() == "ha":
        return round(f * 10000)
    return f if f > 0 else None


def _parse_price(text: str) -> float | None:
    # "477 000 € Hai ..." → premier montant
    m = re.search(r"([\d\s\xa0]{4,})\s*€", text)
    if not m:
        return None
    cleaned = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
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
    print(f"\nTotal Sologne Conseil: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:45]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b.get('pieces') or '?'}p — {b['ville']}"
        )

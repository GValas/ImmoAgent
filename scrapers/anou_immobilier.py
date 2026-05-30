"""scrapers/anou_immobilier.py — Anou Immobilier (réseau Eure-et-Loir / Perche, 28)

Méthode : scrape_simple (httpx) — SSR HTML (CMS « atweb », cartes .atw-card).

Listing : https://anou-immobilier.fr/immobilier.php?recherche_offre=achat&page=N
  - inventaire NATIONAL du réseau (~143 annonces vente), 12 cartes/page, ~12 pages.
  - les autres chemins (/, /a-vendre, /recherche…) sont des catch-all qui renvoient
    la homepage (carrousels) ; la VRAIE page de recherche est immobilier.php.
  - les paramètres recherche_type_bien / recherche_commune sont ignorés côté serveur
    (résolus en JS) → on scrape tout le listing vente et on POST-FILTRE par dept.

Réseau implanté en Eure-et-Loir (28) / Perche → l'essentiel des biens est en 28,
avec quelques débordements limitrophes (41…). Pas de filtre dept serveur fiable
→ POST-FILTRE par code_postal[:2] (extrait du slug d'URL data-favorite-url) :
0 fuite garantie.

Cartes : div.atw-card
  - bouton .favorite-toggle (attributs riches) :
      data-favorite-url   → /immobilier/{type}/a-vendre/{ville-CP5}/{type}-{id}
      data-favorite-city  → ville (MAJUSCULES)
      data-favorite-price → prix entier €
      data-favorite-ref   → référence agence (id_annonce)
      data-favorite-adh    → id adhérent/agence
      data-favorite-title → titre
      data-favorite-image → photo de couverture (.webp)
  - .atw-card-offre  → type de bien (Maison / Appartement / Terrain / Local / Immeuble)
  - .atw-card-tiles > div[title]  →  pièces / chambres / surface int. / surface ext.

On ne garde que maisons / propriétés (exclut appartement, terrain, local, immeuble).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://anou-immobilier.fr"
LISTING_URL = f"{BASE_URL}/immobilier.php"
MAX_PAGES = 20          # plafond de sécurité (~12 pages réelles ; au-delà le site boucle)
PHOTOS_PER_CARD = 1     # une photo de couverture dispo sur la liste

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types conservés (segment d'URL / libellé offre) : maisons / propriétés
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|maison-de-village",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    all_cards = await _fetch_all_cards()

    results: list[dict] = []
    seen: set[str] = set()
    for card in all_cards:
        bien = _parse_card(card)
        if not bien:
            continue

        # POST-FILTRE département via code_postal[:2]
        cp = bien.get("code_postal") or ""
        dept = cp[:2] if len(cp) >= 2 else ""
        if departements and dept not in departements:
            continue
        bien["departement"] = dept

        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        aid = bien.get("id_annonce") or bien.get("url")
        if aid in seen:
            continue
        seen.add(aid)
        results.append(bien)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[AnouImmobilier] Dept {dept}: {n} annonces")

    return results


async def _fetch_all_cards() -> list:
    cards: list = []
    seen_refs: set[str] = set()
    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            params = {"recherche_offre": "achat", "page": page}
            try:
                r = await client.get(LISTING_URL, params=params)
                if r.status_code != 200:
                    break
            except Exception as e:
                print(f"[AnouImmobilier] Erreur page {page}: {e}")
                break

            page_cards = BeautifulSoup(r.text, "html.parser").select("div.atw-card")
            if not page_cards:
                break

            # Le site boucle au-delà de la dernière page (renvoie la dernière) →
            # on s'arrête quand une page n'apporte aucune nouvelle référence.
            new_on_page = 0
            for cd in page_cards:
                ref = _card_ref(cd)
                if ref and ref in seen_refs:
                    continue
                if ref:
                    seen_refs.add(ref)
                cards.append(cd)
                new_on_page += 1
            if new_on_page == 0:
                break

            await asyncio.sleep(0.4)

    return cards


def _card_ref(card) -> str:
    btn = card.select_one(".favorite-toggle")
    if btn:
        return btn.get("data-favorite-ref") or btn.get("data-favorite-url") or ""
    return card.get("id", "")


def _parse_card(card) -> dict | None:
    try:
        btn = card.select_one(".favorite-toggle")

        # URL : priorité au data-favorite-url (canonique), repli sur le lien carte
        url = ""
        if btn:
            url = btn.get("data-favorite-url", "").strip()
        if not url:
            a = card.select_one("a.atw-card-link[href]")
            url = a["href"].strip() if a else ""
        if not url:
            return None

        # Type de bien : segment d'URL /immobilier/{type}/a-vendre/...
        m_type = re.search(r"/immobilier/([a-zé\-]+)/a-vendre/", url, re.IGNORECASE)
        type_seg = m_type.group(1) if m_type else ""
        # Repli : libellé .atw-card-offre
        if not type_seg:
            offre = card.select_one(".atw-card-offre")
            type_seg = offre.get_text(strip=True) if offre else ""
        if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
            return None
        if not _KEEP_TYPE.search(type_seg):
            return None
        type_bien = type_seg.replace("-", " ").strip().lower() or "maison"

        # Code postal : depuis le slug d'URL  …/{ville}-{CP5}/{type}-{id}
        m_cp = re.search(r"/a-vendre/[a-z0-9é\-]+-(\d{5})/", url, re.IGNORECASE)
        code_postal = m_cp.group(1) if m_cp else ""
        if not code_postal:
            m_cp2 = re.search(r"(\d{5})", url)
            code_postal = m_cp2.group(1) if m_cp2 else ""

        # Référence (id_annonce)
        ref = btn.get("data-favorite-ref") if btn else None
        if not ref:
            m_id = re.search(r"-(\d+)$", url)
            ref = m_id.group(1) if m_id else url
        id_annonce = ref

        # Ville
        ville = ""
        if btn:
            ville = (btn.get("data-favorite-city") or "").strip()
        if not ville:
            city_el = card.select_one(".atw-card-city")
            if city_el:
                ville = re.sub(r"\s*-\s*\d{5}.*$", "", city_el.get_text(" ", strip=True)).strip()
        ville = ville.title() if ville.isupper() else ville

        # Titre
        titre = ""
        if btn:
            titre = (btn.get("data-favorite-title") or "").strip()
        if not titre:
            t_el = card.select_one(".atw-card-title")
            titre = t_el.get_text(" ", strip=True) if t_el else ""
        titre = re.sub(r"\s+", " ", titre).strip()
        if not titre:
            titre = f"{type_bien.title()} {ville}".strip()

        # Prix : data-favorite-price (entier) prioritaire
        prix = None
        if btn and btn.get("data-favorite-price"):
            prix = _parse_num(btn["data-favorite-price"])
        if prix is None:
            price_el = card.select_one(".atw-card-price")
            prix = _parse_num(price_el.get_text(" ", strip=True)) if price_el else None

        # Tuiles : pièces / chambres / surface int. / surface ext.
        pieces = chambres = surface = surface_terrain = None
        for tile in card.select(".atw-card-tiles > div"):
            title = (tile.get("title") or "").lower()
            p_el = tile.select_one("p")
            val_txt = p_el.get_text(" ", strip=True) if p_el else ""
            if "pièce" in title or "piece" in title:
                pieces = _parse_int(val_txt)
            elif "chambre" in title:
                chambres = _parse_int(val_txt)
            elif "intérieur" in title or "interieur" in title:
                surface = _parse_num(val_txt)
            elif "extérieur" in title or "exterieur" in title:
                surface_terrain = _parse_num(val_txt)

        # Photo de couverture
        photos: list[str] = []
        if btn and btn.get("data-favorite-image", "").startswith("http"):
            photos.append(btn["data-favorite-image"])
        if not photos:
            img = card.select_one(".atw-card-header img")
            src = img.get("src", "") if img else ""
            if src.startswith("http"):
                photos.append(src)
        photos = photos[:PHOTOS_PER_CARD]

        agence_id = btn.get("data-favorite-adh") if btn else None

        return {
            "source": "anou_immobilier",
            "url": url,
            "id_annonce": id_annonce,
            "titre": titre[:150],
            "type_bien": type_bien,
            "description": None,
            "departement": (code_postal or "")[:2],
            "ville": (ville or "")[:80],
            "code_postal": code_postal,
            "surface": surface,
            "surface_terrain": surface_terrain,
            "pieces": pieces,
            "chambres": chambres,
            "prix": prix,
            "dpe": None,
            "photos": photos,
            "agence": f"Anou Immobilier ({agence_id})" if agence_id else "Anou Immobilier",
        }
    except Exception:
        return None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_num(text: str) -> float | None:
    """'306 800 €' / '163 m²' / '306800' → float"""
    if text is None:
        return None
    cleaned = re.sub(r"[^\d,\.]", "", str(text).replace("\xa0", " ").replace(" ", ""))
    cleaned = cleaned.replace(",", ".")
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(text: str) -> int | None:
    m = re.search(r"(\d+)", text or "")
    return int(m.group(1)) if m else None


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
    print(f"\nTotal Anou Immobilier (depts cibles): {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b.get('pieces') or '?'}p — {b['type_bien']} — {b['ville']}"
        )

"""scrapers/domaine_de_chasse.py — Domaine de Chasse (Les Domaines de France)

Méthode : scrape_simple (httpx) — SSR HTML pur.
Segment : propriétés de chasse / parcs / étangs / forêts / propriétés d'agrément
          (Sologne, Val de Loire). Petit inventaire national de prestige rural.

URL liste paginée : /proprietes-de-chasse.php?offset=N   (N = numéro de page, 1-based)
  → PAS de filtre département côté serveur. Chaque carte affiche son département
    dans le <h3> ("41 - Loir-et-Cher, Propriétés de chasse") → on POST-FILTRE
    strictement sur ce numéro de département (objectif : 0 fuite hors-zone).

URL détail : /vente-proprietes-de-chasse-{slug},{id}.php

Cartes : a.bien (id = "p{id}")
  - href  : page détail
  - h3    : "NN - Dept, Type"  + <span> prix ("2 047 500 euros")
  - h4    : titre
  - ul/li : "Ville : ...", "Superficie : 100 hectares", "Reférence n° 1765"
  - p     : description (peut contenir la surface habitable "205m²")
  - .pics .slide img[src] : photos

Pas de code postal exposé (ni liste ni détail) → le département est déduit du
numéro du <h3>. On renseigne donc `departement` (sûr) et on laisse `code_postal`
à None.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.domainedechasse.fr"
LIST_PATH = "/proprietes-de-chasse.php"
MAX_PAGES = 12
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()
    per_dept: dict[str, int] = {}

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            try:
                r = await client.get(
                    BASE_URL + LIST_PATH, params={"offset": page}
                )
            except Exception as e:
                print(f"[DomaineChasse] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("a.bien")
            if not cards:
                break

            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # POST-FILTRE DÉPARTEMENT STRICT (pas de filtre serveur) → 0 fuite
                if bien["departement"] not in departements:
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
                per_dept[bien["departement"]] = per_dept.get(bien["departement"], 0) + 1

            await asyncio.sleep(0.6)

    for dept in sorted(departements):
        print(f"[DomaineChasse] Dept {dept}: {per_dept.get(dept, 0)} annonces")
    return results


def _parse_card(card) -> dict | None:
    href = card.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

    # id_annonce : id="p912" → 912, sinon ",{id}.php" dans le href
    id_annonce = ""
    cid = card.get("id", "")
    m_id = re.match(r"p?(\d+)$", cid)
    if m_id:
        id_annonce = m_id.group(1)
    if not id_annonce:
        m_h = re.search(r",(\d+)\.php", href)
        if m_h:
            id_annonce = m_h.group(1)
    if not id_annonce:
        id_annonce = url

    # h3 : "NN - Dept name, Type"  + <span> prix
    h3 = card.select_one("h3")
    dept = ""
    type_label = ""
    prix = None
    if h3:
        span = h3.select_one("span")
        if span:
            prix = _parse_price(span.get_text(" ", strip=True))
        # texte du h3 sans le span
        head = h3.get_text(" ", strip=True)
        if span:
            head = head.replace(span.get_text(" ", strip=True), "").strip()
        m_dept = re.match(r"\s*(\d{2,3})\s*-\s*([^,]+?)\s*,\s*(.+)$", head)
        if m_dept:
            dept = m_dept.group(1).zfill(2)
            type_label = m_dept.group(3).strip()
        else:
            m_only = re.match(r"\s*(\d{2,3})\s*-", head)
            if m_only:
                dept = m_only.group(1).zfill(2)
    if not dept:
        return None  # sans département on ne peut pas garantir la zone

    # Titre
    h4 = card.select_one("h4")
    titre = h4.get_text(" ", strip=True) if h4 else ""

    # Liste : Ville / Superficie (hectares) / Référence
    ville = ""
    surface_terrain = None
    ref = ""
    for li in card.select("ul li"):
        txt = li.get_text(" ", strip=True)
        low = txt.lower()
        if low.startswith("ville"):
            ville = re.sub(r"^ville\s*:?\s*", "", txt, flags=re.IGNORECASE).strip()
        elif low.startswith("superficie") or low.startswith("surface"):
            surface_terrain = _parse_hectares(txt)
        elif "ref" in low:
            m_ref = re.search(r"(\d+)", txt)
            if m_ref:
                ref = m_ref.group(1)

    if ref:
        id_annonce = ref

    # Description
    p_el = card.select_one("p")
    description = p_el.get_text(" ", strip=True) if p_el else ""

    # Surface habitable : pas de champ dédié → tentée depuis titre/description
    surface = _parse_surface_hab(description) or _parse_surface_hab(titre)

    if not titre:
        titre = f"{type_label} {ville}".strip() or "Propriété de chasse"

    # Photos
    photos = []
    for img in card.select(".pics img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:"):
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("./"):
            src = f"{BASE_URL}/{src[2:]}"
        elif not src.startswith("http"):
            src = f"{BASE_URL}/{src.lstrip('/')}"
        photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "domaine_de_chasse",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": (type_label or "propriété de chasse").lower()[:60],
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": None,  # jamais exposé par le site
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Domaine de Chasse",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text.replace("euros", ""))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_hectares(text: str) -> float | None:
    """'100 hectares' / '12,5 ha' → m² (1 ha = 10 000 m²)."""
    m = re.search(r"([\d\s\xa0]+(?:[.,]\d+)?)\s*(hectare|ha\b)", text, re.IGNORECASE)
    if not m:
        return None
    val = m.group(1).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(val) * 10_000
    except ValueError:
        return None


def _parse_surface_hab(text: str) -> float | None:
    """Cherche une surface habitable en m² dans le texte libre (ex. '205m²')."""
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0]{0,5})\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 3000:
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
    print(f"\nTotal Domaine de Chasse: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — hab {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

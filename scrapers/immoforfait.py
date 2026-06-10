"""scrapers/immoforfait.py — ImmoForfait (réseau de mandataires immobiliers)

Méthode : scrape_simple (httpx) — SSR HTML, CMS Netty.immo.
MÊME structure de cartes que scrapers/proprietes_rurales.py (div.res_tbl).

Inventaire NATIONAL (~336 biens, 40 départements). Le réseau revendique ~32
départements mais publie en réalité dans toute la France. AUCUN filtre
département côté serveur fiable : /nos-biens?departement=72 est ignoré (renvoie
toujours la 1re page nationale). → On crawle TOUT l'inventaire puis on
POST-FILTRE STRICTEMENT sur le code postal porté par le slug de chaque fiche.

URL pattern (liste) :
    /nos-biens                 → page 1 (121 cartes)
    /nos-biens?start={N}        → fenêtre suivante (incrément de 121)
    On boucle start = 0, 121, 242 … tant que de NOUVELLES cartes apparaissent.

Fiche détail (URL des cartes) :
    /immobilier/{type}-{...}-{ville}-{CP}-fr_{REF}.htm
    ex: /immobilier/maison-ancienne-4-pieces-bonnetable-72110-fr_VM25194.htm
    → CP (5 chiffres avant -fr_) ET ville DANS le slug → filtre dept trivial,
      indépendant du serveur. Le préfixe de type (maison-, immeuble-, terrain-,
      appartement-, stationnement-, fonds-de-commerce-…) permet de ne garder
      que les maisons / propriétés.

Cartes : div.res_tbl[itemtype=schema.org/Offer]
  - URL/titre  : h2 > a[href]            → ...-fr_{REF}.htm
  - réf        : préfixe REF du slug (VM…, LA…, VI…)
  - description: p[itemprop=description]
  - loc/surface: .loc_details            → "Maison Bonnétable 113 m²"
  - prix       : .res_tbl_value[content] (absent/0 = "Nous consulter" → None)
  - image      : a.res_tbl1[style=background-image:url(...)]  (img.netty.immo)

Pièces / surface / CP / ville : extraits du slug (source fiable) avec repli sur
.loc_details pour la surface.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.immoforfait.fr"
LISTING_URL = f"{BASE_URL}/nos-biens"
PAGE_STEP = 121          # taille de fenêtre du paramètre ?start
MAX_WINDOWS = 30         # garde-fou (336 biens ≈ 12 fenêtres réelles)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types (préfixe de slug) à conserver : maisons / propriétés / fermes…
_KEEP_TYPE = re.compile(
    r"^(maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme)",
    re.IGNORECASE,
)
# Types explicitement exclus (apparaissent en tête de slug)
_EXCLUDE_TYPE = re.compile(
    r"^(appartement|terrain|stationnement|garage|parking|immeuble|local|"
    r"commerce|fonds-de-commerce|fonds|immobilier-pro|bureau|loft-atelier|loft)",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0
    cibles = set(departements)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for window in range(MAX_WINDOWS):
            start = window * PAGE_STEP
            url = LISTING_URL if start == 0 else f"{LISTING_URL}?start={start}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[ImmoForfait] Erreur start={start}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.res_tbl")
            if not cards:
                break

            new_refs_this_window = 0
            for card in cards:
                bien = _parse_card(card)
                if not bien:
                    continue

                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue
                # Compte les fiches RÉELLEMENT nouvelles pour décider l'arrêt
                # (la fenêtre suivante répète parfois les dernières cartes).
                new_refs_this_window += 1

                # FILTRE DÉPARTEMENT STRICT : le CP du slug fait foi.
                cp = bien["code_postal"]
                if not cp or cp[:2] not in cibles:
                    seen_ids.add(aid)
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    seen_ids.add(aid)
                    continue
                if prix_min and p and p < prix_min:
                    seen_ids.add(aid)
                    continue
                if surface_min and s and s < surface_min:
                    seen_ids.add(aid)
                    continue

                seen_ids.add(aid)
                results.append(bien)

            # Plus aucune fiche nouvelle → fin de l'inventaire.
            if new_refs_this_window == 0:
                break

            await asyncio.sleep(0.5)

    print(f"[ImmoForfait] {len(results)} annonces retenues (zone cible)")
    return results


def _parse_card(card) -> dict | None:
    a = card.select_one("h2 a[href]") or card.select_one("a.res_tbl1[href]")
    if not a or not a.get("href"):
        return None
    href = a["href"]
    url = href if href.startswith("http") else BASE_URL + href

    slug = href.split("/")[-1]  # {type}-...-{ville}-{CP}-fr_{REF}.htm

    # Réf (id_annonce) : ...-fr_{REF}.htm
    m_ref = re.search(r"fr_([A-Za-z]+\d+)\.htm", slug)
    id_annonce = m_ref.group(1) if m_ref else url

    # Type de bien depuis le préfixe du slug → filtre maisons/propriétés.
    if _EXCLUDE_TYPE.match(slug):
        return None
    if not _KEEP_TYPE.match(slug):
        return None
    m_type = re.match(r"^([a-zàâçéèêëîïôûùüÿñæœ\-]+?)-\d", slug, re.IGNORECASE)
    type_bien = (m_type.group(1).replace("-", " ").strip() if m_type else "maison")

    # CP (5 chiffres juste avant -fr_) + ville (segment précédant le CP).
    code_postal = ""
    ville = None
    m_cp = re.search(r"-(\d{5})-(?:\d{5}-)?fr_", slug)
    if m_cp:
        code_postal = m_cp.group(1)
        # ville = ce qui précède le CP, après le dernier segment "N-pieces" ou "N-m2"
        before = slug[: m_cp.start()]
        seg = re.split(r"\d+-pieces-|\d+-m2-", before)
        ville_slug = seg[-1] if seg else before
        ville_slug = ville_slug.strip("-")
        if ville_slug:
            ville = ville_slug.replace("-", " ").title()
    dept = code_postal[:2] if code_postal else ""

    # Titre
    h2a = card.select_one("h2 a")
    titre = h2a.get_text(" ", strip=True) if h2a else (a.get("title") or "")

    # Localisation / surface affichée : .loc_details "Maison Ville 113 m²"
    loc_el = card.select_one(".loc_details")
    loc_txt = loc_el.get_text(" ", strip=True) if loc_el else ""
    if not ville and loc_txt:
        ville = re.sub(r"\s*\d+\s*m².*$", "", loc_txt).strip() or None

    # Description
    desc_el = card.select_one("p[itemprop=description]")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Prix : .res_tbl_value[content] (0 / absent = "Nous consulter")
    prix = None
    val_el = card.select_one(".res_tbl_value")
    if val_el and val_el.get("content"):
        try:
            v = float(val_el["content"])
            prix = v if v > 0 else None
        except (ValueError, TypeError):
            prix = None

    # Pièces depuis le slug : "...-{N}-pieces-..."
    pieces = None
    m_p = re.search(r"-(\d+)-pieces-", slug)
    if m_p:
        pieces = int(m_p.group(1))

    # Surface habitable : .loc_details "… 113 m²", repli slug "...-{N}-m2-..."
    surface = _parse_surface(loc_txt)
    if surface is None:
        m_s = re.search(r"-(\d+)-m2-", slug)
        if m_s:
            try:
                surface = float(m_s.group(1))
            except ValueError:
                surface = None

    # Image de couverture (background-image:url(...) → img.netty.immo)
    photos = []
    img_a = card.select_one("a.res_tbl1")
    if img_a and img_a.get("style"):
        m_img = re.search(r"url\(([^)]+)\)", img_a["style"])
        if m_img:
            src = m_img.group(1).strip("'\"")
            if src.startswith("http"):
                photos.append(src)

    if not titre:
        titre = f"{type_bien.title()} {ville or ''}".strip()

    return {
        "source": "immoforfait",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": (ville or "")[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,  # non exposé dans la liste
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "ImmoForfait",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_surface(text: str) -> float | None:
    """'Maison Bonnétable 113 m²' → 113.0"""
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0]*)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 2000:
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
    print(f"\nTotal ImmoForfait: {len(biens)} annonces")
    cibles = {str(d).zfill(2) for d in criteres.departements}
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    fuites = [b for b in biens if not b["code_postal"] or b["code_postal"][:2] not in cibles]
    print(f"FUITES hors-département : {len(fuites)}")
    for b in fuites[:5]:
        print(f"  FUITE [{b['code_postal']}] {b['titre'][:50]} — {b['url']}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

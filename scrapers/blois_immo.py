"""scrapers/blois_immo.py — Blois Immo (agence locale, Blois / Loir-et-Cher 41)

Méthode : scrape_simple (httpx) — SSR HTML (CMS immobilier type "office12/bloisi").
URL pattern : /annonces/transaction/Vente.html?page={N}
              → un seul listing "Vente" (l'agence est mono-implantation 41).
              Pas de filtre département côté serveur fiable → on POST-FILTRE
              sur code_postal[:2] (l'inventaire est ~94% en 41, ~6% en 45).

Pagination : ?page=N, 24 biens/page, ~11 pages.

Cartes : div.product
  - URL    : a.product-image[href]  (relatif "../fiches/...")
  - Photos : a.product-image img.photo[src] + img.photo-hidden[src] (relatifs "../office12/...")
  - Titre/Ville : .product-name span  → derniers spans = ville
  - Prix   : .product-price  →  "108 900 €"
  - Pièces : .data-list__item--NbPiece .data-list__item--value
  - Surface: .data-list__item--Surface .data-list__item--value  (m² habitable)
  - Réf    : .data-list__item--products_model .data-list__item--value
  - Type   : déduit de la classe "product--type-{slug}" du conteneur

Code postal : ABSENT des cartes. Reconstruit via la liste déroulante de villes
              de la page (<option>"41000 blois", "45740 lailly en val"…), qui
              mappe nom de ville → CP. Couverture vérifiée = 100% des villes des
              cartes (insensible à la casse). Filtre strict ensuite : on ne garde
              qu'un bien dont code_postal[:2] ∈ départements demandés → 0 fuite.

Types conservés : maisons / propriétés / fermes (on exclut appartements,
                  terrains, locaux, garages, immeubles…).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.blois-immo.fr"
LISTING_URL = f"{BASE_URL}/annonces/transaction/Vente.html"
MAX_PAGES = 15
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Le type est codé dans la classe "product--type-{code}" du conteneur.
# Codes observés (cf. titres) : codes numériques + slugs textuels.
#   1=appartement, 2=maison/grange/bâtisse, 3=demeure, 6=immeuble,
#   10=terrain, 16=maison, fermette, immeublerapport.
# True = on conserve (résidentiel maison/propriété), False = on exclut.
_TYPE_CODE_KEEP: dict[str, bool] = {
    "1": False,   # appartement
    "2": True,    # maison / grange / bâtisse
    "3": True,    # demeure de prestige
    "6": False,   # immeuble de rapport
    "10": False,  # terrain
    "16": True,   # maison
    "fermette": True,
    "immeublerapport": False,
}

# Repli mots-clés sur le titre quand le code est inconnu.
_KEEP_TITLE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|fermette|longere|longère|manoir|"
    r"chateau|château|moulin|demeure|domaine|\bmas\b|gite|gîte|corps de ferme|"
    r"b[âa]tisse|grange|pavillon|ch[âa]let|b[âa]timent",
    re.IGNORECASE,
)
_EXCLUDE_TITLE = re.compile(
    r"appartement|\bterrain\b|local commercial|garage|parking|"
    r"immeuble de rapport|immeuble|bureau|fonds de commerce|\bcave\b|"
    r"cellier|\bbox\b|hangar|studio|viager",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()
    seen_card_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{LISTING_URL}?page={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[BloisImmo] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select("div.product")
            if not cards:
                break

            city2cp = _build_city_cp_map(soup)

            # Détection de fin : le listing "boucle" (re-sert les mêmes cartes)
            # au-delà de la dernière page → si aucune carte nouvelle, on arrête.
            page_card_ids = []
            for card in cards:
                a = card.select_one("a.product-image")
                href = a.get("href", "") if a else ""
                mid = re.search(r"_(\d{5,})/", href)
                page_card_ids.append(mid.group(1) if mid else href)
            if page > 1 and all(cid in seen_card_ids for cid in page_card_ids):
                break
            seen_card_ids.update(page_card_ids)

            for card in cards:
                try:
                    bien = _parse_card(card, city2cp)
                except Exception:
                    continue
                if not bien:
                    continue

                cp = bien["code_postal"]
                # Post-filtre STRICT département → 0 fuite hors-zone.
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
                results.append(bien)

            await asyncio.sleep(0.5)

    # Log par département
    par_dept: dict[str, int] = {}
    for b in results:
        par_dept[b["departement"]] = par_dept.get(b["departement"], 0) + 1
    for d in departements:
        print(f"[BloisImmo] Dept {d}: {par_dept.get(d, 0)} annonces")

    return results


def _build_city_cp_map(soup: BeautifulSoup) -> dict[str, str]:
    """Liste déroulante de villes <option>'41000 blois' → {'blois': '41000'}."""
    mapping: dict[str, str] = {}
    for o in soup.select("option"):
        t = o.get_text(strip=True)
        m = re.match(r"^(\d{5})\s+(.+)$", t)
        if m:
            mapping[m.group(2).strip().lower()] = m.group(1)
    return mapping


def _parse_card(card, city2cp: dict[str, str]) -> dict | None:
    link = card.select_one("a.product-image")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = _abs_url(href)

    # id_annonce : id numérique dans le href (".../4-40-26_60287652/...") ou la réf
    id_num = ""
    m_id = re.search(r"_(\d{5,})/", href)
    if m_id:
        id_num = m_id.group(1)

    # Nom + ville : derniers spans de .product-name
    name_spans = [
        sp.get_text(" ", strip=True) for sp in card.select(".product-name span")
    ]
    name_spans = [s for s in name_spans if s and s != ","]
    ville = name_spans[-1].strip() if name_spans else ""
    titre = name_spans[0].strip() if name_spans else ""

    code_postal = city2cp.get(ville.lower(), "")

    # Type de bien : code dans la classe "product--type-{code}" (table) puis
    # repli sur les mots-clés du titre. On ne garde que le résidentiel.
    classes = card.get("class", []) or []
    type_code = ""
    for c in classes:
        if c.startswith("product--type-"):
            type_code = c[len("product--type-"):]
            break

    keep = _TYPE_CODE_KEEP.get(type_code)
    if keep is None:
        # Code inconnu → décision sur le titre
        if _EXCLUDE_TITLE.search(titre):
            keep = False
        elif _KEEP_TITLE.search(titre):
            keep = True
        else:
            keep = False  # prudence : type ambigu → exclu
    if not keep:
        return None

    # Libellé de type lisible (depuis le titre, sinon générique)
    type_bien = _type_label(titre)

    # Prix : .product-price contient parfois un sous-span "dont X% TTC
    # d'honoraires" → on le retire avant parsing pour ne pas coller les chiffres.
    prix = None
    price_el = card.select_one(".product-price")
    if price_el:
        # Texte direct uniquement (hors sous-spans honoraires/mentions).
        raw = "".join(
            t for t in price_el.find_all(string=True, recursive=False)
        )
        if not raw.strip():
            # repli : couper avant "dont ... honoraires"
            raw = re.split(r"\bdont\b", price_el.get_text(" ", strip=True))[0]
        prix = _parse_price(raw)

    # Pièces / surface
    pieces = _data_int(card, "NbPiece")
    surface = _data_float(card, "Surface")
    chambres = _data_int(card, "NbChambre")
    surface_terrain = _data_float(card, "SurfaceTerrain")

    # Référence
    ref = ""
    ref_el = card.select_one(
        ".data-list__item--products_model .data-list__item--value"
    )
    if ref_el:
        ref = ref_el.get_text(strip=True)

    id_annonce = id_num or ref or url

    # Photos (relatives "../office12/...")
    photos = []
    for img in card.select("a.product-image img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(_abs_url(src))
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "blois_immo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2] if code_postal else None,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Blois Immo",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _type_label(titre: str) -> str:
    """Déduit un libellé de type lisible depuis le titre de l'annonce."""
    m = _KEEP_TITLE.search(titre or "")
    if m:
        return m.group(0).lower()
    return "maison"


def _abs_url(href: str) -> str:
    if href.startswith("http"):
        return href
    cleaned = href.lstrip("./")
    if cleaned.startswith("/"):
        return BASE_URL + cleaned
    return f"{BASE_URL}/{cleaned}"


def _data_int(card, kind: str) -> int | None:
    el = card.select_one(
        f".data-list__item--{kind} .data-list__item--value"
    )
    if not el:
        return None
    m = re.search(r"\d+", el.get_text())
    return int(m.group(0)) if m else None


def _data_float(card, kind: str) -> float | None:
    el = card.select_one(
        f".data-list__item--{kind} .data-list__item--value"
    )
    if not el:
        return None
    txt = el.get_text().replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", txt)
    return float(m.group(0)) if m else None


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text)
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
    print(f"\nTotal Blois Immo: {len(biens)} annonces")
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

"""scrapers/gautard_immobilier.py — Gautard Immobilier (agence mono-37, Tours)

Méthode : scrape_simple (httpx) — SSR HTML statique, aucun JS, aucun anti-bot.
URL pattern : /avendre/          (page 1)
              /avendre/vente-{N}.html   (pages 2..N)
              → la pagination s'arrête quand le serveur renvoie 300/!=200
                ou qu'il n'y a plus de carte (3 pages observées, ~33 biens).

Cartes : div.card (un <a> englobant vers la page détail sur location37.fr)
  - URL   : a[href]               → https://location37.fr/BIEN/property/...
  - Prix  : .price span           → "399.000" (+ "€" hors du span)
  - Statut: .status .meta-list    → "Acheter" (on ne garde que la vente)
  - Image : .img-wrap img[src]    → relatif (img/...) → préfixé BASE_URL/avendre/
  - Titre : .content-wrap .title
  - Ville : .content-wrap > ul.meta-list (1er) → "Tours", "St Cyr-sur-Loire"...
  - Méta  : .meta-box-list        → surface ("118 m2"), pièces (.right person1),
                                     garages (.right car95)
  - Réf   : texte "Réf : GI-SG-V491"

Filtre département : l'agence est strictement tourangelle (Indre-et-Loire).
  Pas de code postal dans la carte → on déduit le CP via CITY_CP (communes 37
  observées) et on force le département à 37. Post-filtre STRICT : tout bien
  dont le département déduit n'est pas dans les départements cibles est écarté
  → 0 fuite hors-zone garantie.

NB : portail lié à vente37.fr (même agence Gautard) déjà en sources ; recouvrement
     partiel possible du fonds d'annonces. La déduplication inter-sources du
     hunter (prix+surface+ville) absorbe les doublons éventuels.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import unicodedata

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://gautard-immobilier.fr"
LIST_PATH = "/avendre/"
MAX_PAGES = 8
PHOTOS_PER_CARD = 6

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Agence strictement Indre-et-Loire (37). Mapping commune (normalisée) → CP.
# Permet de poser un code_postal cohérent et de garantir le filtre département.
CITY_CP: dict[str, str] = {
    "tours": "37000",
    "tours centre": "37000",
    "tours nord": "37100",
    "tours-nord": "37100",
    "tours sud": "37200",
    "joue les tours": "37300",
    "joue-les-tours": "37300",
    "st cyr sur loire": "37540",
    "saint cyr sur loire": "37540",
    "saint-cyr-sur-loire": "37540",
    "st-cyr-sur-loire": "37540",
    "mettray": "37390",
    "montlouis sur loire": "37270",
    "montlouis-sur-loire": "37270",
    "vouvray": "37210",
    "rochecorbon": "37210",
    "fondettes": "37230",
    "la riche": "37520",
    "chambray les tours": "37170",
    "chambray-les-tours": "37170",
    "esvres sur indre": "37320",
    "esvres-sur-indre": "37320",
    "ballan mire": "37510",
    "ballan-mire": "37510",
    "luynes": "37230",
    "amboise": "37400",
    "veretz": "37270",
    "la membrolle sur choisille": "37390",
    "notre dame d oe": "37390",
}

# Libellé générique « Sur Tours et en Indre-et-Loire » → on rattache au 37.
_GENERIC_37 = re.compile(r"indre[- ]et[- ]loire", re.IGNORECASE)


def _norm(s: str) -> str:
    """minuscule, sans accents, espaces compactés (pour le mapping commune→CP)."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower().replace("'", " ")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # L'agence ne couvre que le 37 : inutile de requêter si 37 n'est pas ciblé.
    if departements and "37" not in departements:
        print("[Gautard] Dept 37 hors cibles → 0 annonce")
        return []

    results: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = (
                BASE_URL + LIST_PATH
                if page == 1
                else f"{BASE_URL}{LIST_PATH}vente-{page}.html"
            )
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Gautard] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.card")
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

                # Post-filtre STRICT département (0 fuite)
                dept = bien["departement"]
                if departements and dept not in departements:
                    continue

                aid = bien["id_annonce"]
                if aid in seen:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                seen.add(aid)
                results.append(bien)
                new_on_page += 1

            print(f"[Gautard] Page {page}: {new_on_page} annonces retenues")
            # On ne coupe pas sur une page sans match : le filtre prix/surface
            # peut vider une page alors que les suivantes ont du stock. La boucle
            # s'arrête d'elle-même sur status != 200 (300 = fin) ou 0 carte.
            await asyncio.sleep(0.5)

    print(f"[Gautard] Total: {len(results)} annonces")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + LIST_PATH + href.lstrip("/")

    # Écarte les cartes de navigation / promo (pas de vraie annonce) :
    # liens vers une racine de domaine, une recherche, ou un libellé "déjà vendus".
    if re.search(r"property-search|s-status=", href, re.IGNORECASE):
        return None
    if re.fullmatch(r"https?://[^/]+/?", href.strip()):
        return None
    blob_card = card.get_text(" ", strip=True).lower()
    if "déjà vendu" in blob_card or "deja vendu" in blob_card:
        return None
    if "découvrez tous les biens" in blob_card or "decouvrez tous les biens" in blob_card:
        return None

    # Statut : ne garder que la vente ("Acheter")
    status_el = card.select_one(".status")
    status = status_el.get_text(" ", strip=True).lower() if status_el else ""
    if status and "louer" in status:
        return None

    # Ville : 1er ul.meta-list dans .content-wrap
    cw = card.select_one(".content-wrap")
    ville_raw = ""
    if cw:
        ml = cw.select_one("ul.meta-list")
        if ml:
            ville_raw = ml.get_text(" ", strip=True)
    code_postal, dept = _resolve_loc(ville_raw)
    ville = _clean_ville(ville_raw)

    # Titre
    title_el = card.select_one(".content-wrap .title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"Bien {ville}".strip()

    # Prix : .price span
    price_el = card.select_one(".price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Méta-box : surface / pièces / garages
    box = card.select_one(".meta-box-list")
    surface = pieces = chambres = None
    if box:
        box_text = box.get_text(" ", strip=True)
        surface = _parse_surface(box_text)
        # pièces : nombre à côté de l'icône flaticon-person1
        pieces = _icon_value(box, "flaticon-person1")

    # Référence (id_annonce)
    ref = ""
    m_ref = re.search(r"R[ée]f\s*:\s*([A-Za-z0-9\-/]+)", card.get_text(" ", strip=True))
    if m_ref:
        ref = m_ref.group(1)
    if not ref:
        m_url = re.search(r"ref-([a-z0-9\-]+)/?$", href, re.IGNORECASE)
        ref = m_url.group(1) if m_url else url
    id_annonce = ref

    # Type de bien depuis le titre / l'url
    type_bien = _type_from(titre, href)

    # Photo (relative au dossier /avendre/)
    photos = []
    img = card.select_one(".img-wrap img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("http"):
                photos.append(src)
            elif src.startswith("//"):
                photos.append("https:" + src)
            else:
                photos.append(BASE_URL + LIST_PATH + src.lstrip("/"))
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "gautard_immobilier",
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
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Gautard Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_loc(ville_raw: str) -> tuple[str, str]:
    """Déduit (code_postal, departement) depuis le libellé commune.

    Mapping CITY_CP sur communes 37 connues ; repli sur '37' pour le libellé
    générique 'Indre-et-Loire'. Renvoie ('', '') si non résoluble (le bien
    sera alors écarté par le post-filtre, garantissant 0 fuite)."""
    key = _norm(ville_raw)
    if key in CITY_CP:
        cp = CITY_CP[key]
        return cp, cp[:2]
    if _GENERIC_37.search(ville_raw):
        return "", "37"
    # Tentative : un préfixe connu (ex: 'tours centre' déjà couvert ; 'tours x')
    for name, cp in CITY_CP.items():
        if key.startswith(name + " ") or key == name:
            return cp, cp[:2]
    return "", ""


def _clean_ville(ville_raw: str) -> str:
    v = re.sub(r"\s+", " ", ville_raw).strip()
    return v


def _parse_price(text: str) -> float | None:
    """'399.000 €' / '577.000' → 399000.0 (le point est un séparateur de milliers)."""
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    # Retire les séparateurs de milliers (points / espaces déjà ôtés)
    cleaned = cleaned.replace(".", "")
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'118 m2' / '134,60 m2' → 118.0 / 134.6"""
    m = re.search(r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m", text)
    if not m:
        return None
    val = m.group(1).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        f = float(val)
        return f if 5 <= f <= 5000 else None
    except ValueError:
        return None


def _icon_value(box, icon_class: str) -> int | None:
    """Renvoie l'entier qui suit l'icône icon_class dans la meta-box."""
    icon = box.select_one(f"i.{icon_class}")
    if not icon:
        return None
    nxt = icon.next_sibling
    while nxt is not None:
        txt = nxt.get_text(strip=True) if hasattr(nxt, "get_text") else str(nxt).strip()
        m = re.search(r"\d+", txt or "")
        if m:
            return int(m.group(0))
        nxt = nxt.next_sibling
    return None


def _type_from(titre: str, href: str) -> str:
    blob = f"{titre} {href}".lower()
    if "appartement" in blob:
        return "appartement"
    if "studio" in blob:
        return "appartement"
    if "terrain" in blob:
        return "terrain"
    if "immeuble" in blob:
        return "immeuble"
    if "demeure" in blob or "maison" in blob:
        return "maison"
    return "maison"


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
    print(f"\nTotal Gautard Immobilier: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal'] or b['departement']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

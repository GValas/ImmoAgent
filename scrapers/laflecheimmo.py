"""scrapers/laflecheimmo.py — La Flèche Immobilier (agence indépendante, La Flèche 72)

Méthode : scrape_simple (httpx) — SSR HTML (plateforme La Boîte Immo / staticlbi).
URL pattern : /a-vendre/{N}   (liste paginée, ~9 biens/page, pas de filtre dept dans l'URL)
              → on scrape la liste nationale de l'agence et on POST-FILTRE par code_postal[:2].

Cartes : <article itemscope itemtype="https://schema.org/Product">
  - URL    : meta[itemprop=url] (content="/{id}-slug.html")  ou  a[href]
  - Type   : h1[itemprop=name]  (ex "Maison", "Maison de village", "Terrain à bâtir")
  - Prix   : span[itemprop=price][content]  (ex content="136500")
  - Réf    : span[itemprop=productID]  (ex "Ref 5940")
  - Loc    : img[itemprop=image][alt]  →  "Offres de vente {type} {ville} {cp}"
  - Texte  : div.top-content p  (description courte)
  - Photo  : img[itemprop=image][src]  (//laflecheimmo.staticlbi.com/...)

Le département vient du CP encodé dans l'alt de l'image (5 chiffres en fin de chaîne).
Post-filtre STRICT code_postal[:2] ∈ départements cibles → 0 fuite (couverture réelle 72/49/53).

Enrichissement (surface / surface_terrain / pièces / chambres / DPE) : page détail SSR
  p.data > span.termInfos (label) + span.valueInfos (valeur), sur les seuls biens retenus.
  DPE : img[alt=DPE] src=/admin/dpe.php?...&idann={id} (pas de lettre exploitable → None).

Particularités : agence mono-implantation, faible volume (~30 biens) mais stock réel
  dans la zone cible ; pagination s'arrête quand une page ne renvoie plus d'<article>.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.laflecheimmo.com"
MAX_PAGES = 15
PHOTOS_PER_CARD = 10
ENRICH = True  # va chercher surface/pièces/terrain/DPE sur la page détail des biens retenus

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Catégories canoniques telles qu'encodées dans l'alt de l'image
# ("Offres de vente {TYPE} {VILLE} {CP}"). Ordre = du plus long au plus court
# pour matcher "Maison de village" avant "Maison".
_KNOWN_TYPES = [
    "Maison de village",
    "Maison de ville",
    "Maison",
    "Pavillon",
    "Fermette",
    "Ferme",
    "Longère",
    "Longere",
    "Propriété",
    "Propriete",
    "Manoir",
    "Château",
    "Chateau",
    "Demeure",
    "Villa",
    "Moulin",
    "Gîte",
    "Gite",
    "Corps de ferme",
    "Terrain à batir",
    "Terrain à bâtir",
    "Terrain",
    "Appartement",
    "Immeuble",
    "Local",
    "Garage",
    "Parking",
]

# Types de bien à exclure (on garde maisons / fermettes / propriétés / village...)
_EXCLUDE_TYPE = re.compile(
    r"terrain|appartement|immeuble|local|commerce|garage|parking|bureau|"
    r"fonds|stationnement|cave",
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
            url = f"{BASE_URL}/a-vendre/{page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[LaFlecheImmo] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                'article[itemtype$="schema.org/Product"]'
            )
            if not cards:
                break  # plus d'annonces → fin de pagination

            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # Post-filtre département STRICT (0 fuite)
                cp = bien["code_postal"]
                if not cp or cp[:2] not in departements:
                    continue
                bien["departement"] = cp[:2]

                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue

                # Bornes prix (sur champ connu uniquement)
                p = bien.get("prix") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue

                seen_ids.add(aid)
                results.append(bien)

            await asyncio.sleep(0.5)

        # Enrichissement détail (surface / pièces / terrain / DPE) sur les biens retenus
        if ENRICH and results:
            for bien in results:
                try:
                    await _enrich_detail(client, bien)
                except Exception:
                    pass
                await asyncio.sleep(0.4)

    # Filtre surface après enrichissement (sans exclure un bien à surface inconnue)
    if surface_min:
        results = [
            b for b in results
            if not b.get("surface") or b["surface"] >= surface_min
        ]

    print(f"[LaFlecheImmo] {len(results)} annonces retenues (zone cible)")
    return results


def _parse_card(card) -> dict | None:
    # URL détail
    meta_url = card.select_one('meta[itemprop="url"]')
    href = ""
    if meta_url and meta_url.get("content"):
        href = meta_url["content"]
    else:
        a = card.select_one("a[href]")
        href = a.get("href", "") if a else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce : préférer la référence affichée, sinon l'id du slug d'URL
    ref_el = card.select_one('[itemprop="productID"]')
    ref = ""
    if ref_el:
        ref = re.sub(r"\s*Ref\s*", "", ref_el.get_text(" ", strip=True)).strip()
    m_id = re.search(r"/(\d+)-", href)
    url_id = m_id.group(1) if m_id else ""
    id_annonce = ref or url_id or url

    # Catégorie + localisation depuis l'alt de l'image :
    # "Offres de vente {TYPE canonique} {ville} {cp}"
    img = card.select_one('img[itemprop="image"]') or card.select_one("img")
    alt = img.get("alt", "") if img else ""
    type_bien, ville, code_postal = _parse_alt(alt)

    # Filtre type : on écarte terrains / appartements / immeubles...
    if not type_bien or _EXCLUDE_TYPE.search(type_bien):
        return None

    # Prix
    price_el = card.select_one('[itemprop="price"]')
    prix = None
    if price_el:
        raw = price_el.get("content") or price_el.get_text(" ", strip=True)
        prix = _parse_price(raw)

    # Titre = libellé libre de l'annonce (h1), enrichi de la ville
    name_el = card.select_one('h1[itemprop="name"]') or card.select_one(
        '[itemprop="name"]'
    )
    titre = (name_el.get_text(" ", strip=True) if name_el else "").strip()
    if ville and ville.lower() not in titre.lower():
        titre = f"{titre} - {ville}".strip(" -")
    if not titre:
        titre = f"{type_bien} {ville}".strip()

    # Description courte
    desc_el = card.select_one(".top-content")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Photo de la carte
    photos = []
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)

    return {
        "source": "laflecheimmo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": None,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "La Flèche Immobilier",
    }


async def _enrich_detail(client: httpx.AsyncClient, bien: dict) -> None:
    """Complète surface / surface_terrain / pièces / chambres depuis la page détail SSR."""
    r = await client.get(bien["url"])
    if r.status_code != 200:
        return
    soup = BeautifulSoup(r.text, "html.parser")

    pairs: dict[str, str] = {}
    for p in soup.select("p.data"):
        lab = p.select_one(".termInfos")
        val = p.select_one(".valueInfos")
        if lab and val:
            pairs[lab.get_text(" ", strip=True).lower()] = val.get_text(
                " ", strip=True
            )

    for label, value in pairs.items():
        if "surface habitable" in label and bien.get("surface") is None:
            bien["surface"] = _parse_metric(value)
        elif "surface terrain" in label or "surface du terrain" in label:
            bien["surface_terrain"] = _parse_metric(value)
        elif "nombre de pi" in label:
            bien["pieces"] = _parse_int_val(value)
        elif "nombre de chambre" in label:
            bien["chambres"] = _parse_int_val(value)

    # Plus de photos depuis la galerie détail (images/biens/ = vraies photos du bien)
    extra: list[str] = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "staticlbi.com" in src and "/images/biens/" in src:
            if src.startswith("//"):
                src = "https:" + src
            if src not in extra:
                extra.append(src)
    if extra:
        bien["photos"] = extra[:PHOTOS_PER_CARD]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_alt(alt: str) -> tuple[str, str, str]:
    """'Offres de vente Maison de village Clefs 49150'
        → ('maison de village', 'Clefs', '49150')."""
    cp = ""
    m_cp = re.search(r"(\d{5})\s*$", alt.strip())
    if m_cp:
        cp = m_cp.group(1)
    s = re.sub(r"^Offres de vente\s*", "", alt, flags=re.IGNORECASE)
    s = re.sub(r"\s*\d{5}\s*$", "", s).strip()

    # Identifie la catégorie canonique en tête (plus long match d'abord)
    type_bien = ""
    ville = s
    for cat in _KNOWN_TYPES:
        if s.lower().startswith(cat.lower()):
            type_bien = cat.lower()
            ville = s[len(cat):].strip()
            break
    return type_bien, ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", str(text)).replace(",", ".")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_metric(text: str) -> float | None:
    """'111,59 m²' → 111.59 ; '45 m²' → 45.0 ; '1,05 ha' → 10500.0."""
    s = str(text)
    m = re.search(r"([\d\s\xa0]+(?:[.,]\d+)?)", s)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
    try:
        f = float(val)
    except ValueError:
        return None
    # Conversion hectares / ares → m²
    if re.search(r"\bha\b|hectare", s, re.IGNORECASE):
        f *= 10000
    elif re.search(r"\ba\b|\bare\b", s, re.IGNORECASE) and "m" not in s.lower():
        f *= 100
    return f if 0 < f <= 10_000_000 else None


def _parse_int_val(text: str) -> int | None:
    m = re.search(r"(\d+)", str(text))
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
    print(f"\nTotal La Flèche Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b.get('pieces') or '?'}p — {b['type_bien']} — {b['ville']}"
        )

"""scrapers/colbert_immobilier89.py — Colbert Immobilier (réseau local Yonne 89)

Méthode : scrape_simple (httpx) — SSR HTML (Apache / Symfony-like, cartes a.property)
Réseau indépendant de 3 agences : Auxerre (89000), Monéteau (89470),
Seignelay (89250). Agence mono-département : tous les biens sont dans l'Yonne (89).

URL pattern liste :
  page 1 : /immobilier/vente
  page N : /immobilier/vente{N}     (ex: /immobilier/vente2 … /immobilier/vente6)
  → ~71 biens en vente, 12 par page, pas de filtre département côté serveur
    (inutile : tout est dans le 89). On post-filtre quand même code_postal[:2]=='89'.

Cartes : a.property
  - URL    : href de la balise a  → /immobilier/annonce-{slug}-{ref}
  - Prix   : .price               → "51 000 €"
  - Photo  : .photo (background-image:url('…'))
  - Titre  : h3.name
  - Ville  : .location            → nom de ville seul (PAS de code postal)
  - Détails: .details .info        (attribut title = "Nb de pièces" / "Surface m²" /
             "Terrain m²"), valeur dans .number ("-" si absent)
  - Réf    : dernier segment du slug d'URL (ex: 779aux)

Code postal : ABSENT de la liste (la carte ne donne que la ville). On le récupère
sur la page détail, dont le H1 contient "Ville (CP)" (ex: "Tonnerre (89700)").
Une requête détail par bien (volume faible, ~71) → permet le post-filtre strict
code_postal[:2]=='89' (objectif 0 fuite).

Type de bien : déduit du titre de la carte. On exclut terrains / locaux / parkings.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.colbertimmobilier89.com"
MAX_PAGES = 8
PHOTOS_PER_CARD = 10
DEPT = "89"  # agence mono-département (Yonne)


# Types à exclure (déduits du titre de la carte)
_EXCLUDE_TYPE = re.compile(
    r"terrain|local|commerce|garage|parking|bureau|fonds\s+de\s+commerce|"
    r"droit\s+au\s+bail",
    re.IGNORECASE,
)
# Mapping titre → type_bien normalisé
_TYPE_PATTERNS = [
    (re.compile(r"maison\s+de\s+village", re.I), "maison de village"),
    (re.compile(r"propri[ée]t[ée]", re.I), "propriété"),
    (re.compile(r"villa", re.I), "villa"),
    (re.compile(r"pavillon", re.I), "maison"),
    (re.compile(r"maison|longère|longere|fermette|ferme|corps\s+de\s+ferme", re.I), "maison"),
    (re.compile(r"loft", re.I), "appartement"),
    (re.compile(r"appartement|studio|duplex", re.I), "appartement"),
    (re.compile(r"immeuble|ensemble\s+immobilier", re.I), "immeuble"),
]


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Agence mono-89 : si 89 n'est pas une cible, rien à faire
    if DEPT not in departements:
        print(f"[Colbert89] Dept {DEPT} hors zone cible → 0 annonce")
        return []

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        cards = await _collect_cards(client)
        print(f"[Colbert89] {len(cards)} cartes en vente trouvées")

        for card in cards:
            try:
                bien = await _build_bien(client, card)
            except Exception as e:
                print(f"[Colbert89] Erreur carte : {e}")
                continue
            if not bien:
                continue

            # Post-filtre département STRICT (objectif 0 fuite)
            if not bien["code_postal"] or bien["code_postal"][:2] != DEPT:
                continue

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            results.append(bien)
            await asyncio.sleep(0.5)

    print(f"[Colbert89] Dept {DEPT}: {len(results)} annonces retenues")
    return results


async def _collect_cards(client: httpx.AsyncClient) -> list:
    """Parcourt les pages de listing et renvoie les balises a.property uniques."""
    cards = []
    seen_urls: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        suffix = "vente" if page == 1 else f"vente{page}"
        url = f"{BASE_URL}/immobilier/{suffix}"
        r = await client.get(url)
        if r.status_code != 200:
            break
        page_cards = BeautifulSoup(r.text, "html.parser").select("a.property")
        if not page_cards:
            break
        new = 0
        for c in page_cards:
            href = c.get("href", "")
            if not href or href in seen_urls:
                continue
            seen_urls.add(href)
            cards.append(c)
            new += 1
        if new == 0:
            break
        await asyncio.sleep(0.5)
    return cards


async def _build_bien(client: httpx.AsyncClient, card) -> dict | None:
    href = card.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Référence = dernier segment du slug
    ref = href.rstrip("/").split("-")[-1].strip()
    id_annonce = ref or url

    title_el = card.select_one("h3.name") or card.select_one(".name")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    # Exclusion des terrains / locaux / parkings
    if _EXCLUDE_TYPE.search(titre):
        return None
    type_bien = _detect_type(titre)
    if not type_bien:
        return None

    # Ville (carte) — sans code postal
    loc_el = card.select_one(".location")
    ville_card = loc_el.get_text(" ", strip=True) if loc_el else ""

    # Prix
    price_el = card.select_one(".price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Détails : pièces / surface / terrain (via l'attribut title)
    pieces = surface = surface_terrain = None
    for info in card.select(".details .info"):
        title_attr = (info.get("title") or "").lower()
        num_el = info.select_one(".number")
        val = _parse_num(num_el.get_text(strip=True) if num_el else "")
        if val is None:
            continue
        if "pièce" in title_attr or "piece" in title_attr:
            pieces = int(val)
        elif "surface" in title_attr:
            surface = val
        elif "terrain" in title_attr:
            surface_terrain = val

    # Photo (background-image de .photo)
    photos = []
    photo_el = card.select_one(".photo")
    if photo_el:
        style = photo_el.get("style", "")
        m = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", style)
        if m:
            photos.append(m.group(1))

    # Page détail → code postal (H1 "Ville (CP)") + description + DPE
    ville_detail, code_postal, description, dpe, det_photos = await _scrape_detail(
        client, url
    )
    ville = ville_detail or ville_card
    for ph in det_photos:
        if ph not in photos:
            photos.append(ph)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "colbert_immobilier89",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": (description or "")[:1200],
        "departement": DEPT,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Colbert Immobilier",
    }


async def _scrape_detail(client: httpx.AsyncClient, url: str):
    """Récupère ville, code postal (H1 'Ville (CP)'), description, DPE, photos."""
    ville = code_postal = description = dpe = ""
    photos: list[str] = []
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return ville, code_postal, description, dpe, photos
        soup = BeautifulSoup(r.text, "html.parser")

        h1 = soup.select_one("h1")
        h1_txt = h1.get_text(" ", strip=True) if h1 else ""
        # "51 000 € * TONNERRE CENTRE VILLE - Tonnerre (89700) Réf. 779aux ..."
        # Le H1 a la forme "... - <Ville> (<CP>)" : on prend le segment qui
        # précède immédiatement le CP, après le dernier tiret.
        m = re.search(r"([A-Za-zÀ-ÿ'’\-\s]+?)\s*\((\d{5})\)", h1_txt)
        if m:
            ville = m.group(1)
            # ne garder que ce qui suit le dernier " - " (titre commercial avant)
            if " - " in ville:
                ville = ville.rsplit(" - ", 1)[-1]
            ville = ville.strip().strip("-").strip()
            code_postal = m.group(2)
        else:
            # Repli : un CP 89xxx présent dans le bloc principal
            m2 = re.search(r"\b(89\d{3})\b", r.text)
            if m2:
                code_postal = m2.group(1)

        # Description : meta description ou bloc texte principal
        meta = soup.select_one('meta[name="description"]')
        if meta and meta.get("content"):
            description = meta["content"]

        # DPE : lettre A-G près de "DPE" / "classe énergie"
        m_dpe = re.search(
            r"(?:DPE|classe\s+énerg)[^A-G]{0,40}\b([A-G])\b", r.text, re.IGNORECASE
        )
        if m_dpe:
            dpe = m_dpe.group(1).upper()

        # Photos additionnelles (galerie : background-image ou img)
        for el in soup.select(".photo, .slide, .gallery img, .carousel img"):
            src = ""
            style = el.get("style", "")
            ms = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", style)
            if ms:
                src = ms.group(1)
            elif el.name == "img":
                src = el.get("data-src") or el.get("src") or ""
            if src and "staticlbi.com" in src and src not in photos:
                photos.append(src)
    except Exception as e:
        print(f"[Colbert89] détail {url}: {e}")
    return ville, code_postal, description, dpe, photos


# ── Helpers ──────────────────────────────────────────────────────────────────

def _detect_type(titre: str) -> str | None:
    for pat, label in _TYPE_PATTERNS:
        if pat.search(titre):
            return label
    # Type non identifié et non exclu → maison par défaut (réseau résidentiel)
    return "maison"


def _parse_num(text: str) -> float | None:
    """'1398' → 1398.0 ; '-' → None ; '3' → 3.0"""
    if not text or text.strip() in ("-", ""):
        return None
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", "."))
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
    print(f"\nTotal Colbert Immobilier 89: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

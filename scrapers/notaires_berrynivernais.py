"""scrapers/notaires_berrynivernais.py — Chambre des notaires Berry-Nivernais

Portail notarial régional interdépartemental couvrant le Cher (18), l'Indre (36)
et la Nièvre (58). Site SSR (CMS Prisme/Novius) : les annonces sont déjà dans le
HTML brut, pas de JS requis → httpx pur.

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /petites-annonces?page=N   (1..MAX_PAGES, ~12 cartes/page)
              → AUCUN filtre département côté serveur. Le portail ne sert que ses
                3 départements (18/36/58) mêlés. Filtre dept = POST-FILTRE STRICT.

Filtre département (0 fuite) :
  - chaque carte pointe vers immobilier.notaires.fr/.../{ville}-{NN}/{id}
    → le code dept (NN) est le suffixe du slug commune ;
  - redondance : la ligne de localisation "Ville - Departement (NN)" porte aussi
    le code entre parenthèses.
  On retient le NN du slug, on le recoupe avec celui de la ligne loc, et on
  n'accepte la carte que si NN ∈ départements cibles couverts.

Cartes : a.offer-card
  - URL    : a.offer-card[href]  (lien externe vers immobilier.notaires.fr)
  - Prix   : .offer-card__content > p (1ʳᵉ) → "190000 €" (prix charge acquéreur)
  - Type   : .offer-card__content p.uppercase → "Vente - Maison / villa"
  - Loc    : .offer-card__content .text-gray-600 > p (1ʳᵉ) → "Aubigny-sur-Nère - Cher (18)"
  - Surf/pc: .offer-card__content .text-gray-600 > p (2ᵉ)  → "84.56m2 - 7 pièces"
  - Titre  : .offer-card__content p.text-2xs (description courte)
  - Photo  : img.image[src]

Type de bien : on ne garde que maisons / propriétés / fermes…, on exclut
               appartements / terrains / locaux.

Couverture : strictement 18/36/58 (les autres départements cibles → 0 bien, normal).
             ~44 pages × ~12 cartes au moment du test (2026-06-09).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://chambredesnotaires-berrynivernais.notaires.fr"
LIST_PATH = "/petites-annonces"
MAX_PAGES = 50
PHOTOS_PER_CARD = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Départements réellement servis par ce portail interdépartemental.
DEPTS_COUVERTS = {"18", "36", "58"}

# Types de bien (libellé carte) à conserver : maisons / propriétés / fermes...
_KEEP_TYPE = re.compile(
    r"maison|villa|propri[eé]t[eé]|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|mas|g[iî]te|corps de ferme|maison de village|pavillon",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|viager|cave|grange seule|hangar",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Intersection avec les départements réellement couverts par le portail.
    cibles = departements & DEPTS_COUVERTS
    if not cibles:
        print(
            "[NotairesBerryNivernais] Aucun département cible couvert "
            f"(portail = {sorted(DEPTS_COUVERTS)}) → 0 annonce"
        )
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}{LIST_PATH}?page={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[NotairesBerryNivernais] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("a.offer-card")
            if not cards:
                break

            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                dept = bien["departement"]
                # POST-FILTRE STRICT — 0 fuite hors-zone
                if dept not in cibles:
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

            # dernière page atteinte (moins d'une page pleine) → on arrête
            if len(cards) < 10:
                break

    print(f"[NotairesBerryNivernais] {len(results)} annonces (depts {sorted(cibles)})")
    return results


def _parse_card(card) -> dict | None:
    href = card.get("href", "")
    if not href or "immobilier.notaires.fr" not in href:
        return None
    url = href

    content = card.select_one(".offer-card__content") or card

    # Type de bien : "Vente - Maison / villa"
    type_el = content.select_one("p.uppercase")
    type_raw = type_el.get_text(" ", strip=True) if type_el else ""
    type_clean = re.sub(r"^\s*vente\s*-\s*", "", type_raw, flags=re.IGNORECASE).strip()
    type_clean = re.sub(r"\s+", " ", type_clean)
    if _EXCLUDE_TYPE.search(type_clean) and not _KEEP_TYPE.search(type_clean):
        return None
    if not _KEEP_TYPE.search(type_clean):
        return None
    type_bien = type_clean.split("/")[0].strip().lower() or "maison"

    # Localisation : "Aubigny-sur-Nère - Cher (18)"
    loc_ps = content.select(".text-gray-600 > p")
    loc_text = loc_ps[0].get_text(" ", strip=True) if loc_ps else ""
    ville, dept_loc = _parse_loc(loc_text)

    # Département depuis le slug URL : .../{ville}-{NN}/{id}
    dept_slug = ""
    m_slug = re.search(r"-(\d{2,3})/\d+\b", href)
    if m_slug:
        dept_slug = m_slug.group(1)[:2]

    dept = dept_slug or dept_loc
    if not dept:
        return None
    # Si les deux sources divergent, on refuse (prudence anti-fuite)
    if dept_slug and dept_loc and dept_slug != dept_loc:
        return None

    # id_annonce : id numérique terminal du slug
    m_id = re.search(r"/(\d+)\b/?$", href.rstrip("/"))
    id_annonce = m_id.group(1) if m_id else url

    # Surface / pièces : "84.56m2 - 7 pièces"
    sp_text = loc_ps[1].get_text(" ", strip=True) if len(loc_ps) > 1 else ""
    surface = _parse_surface(sp_text)
    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", sp_text)

    # Prix : 1ᵉʳ <p> du contenu → "190000 €"
    price_el = content.select_one("p")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Titre / description courte
    desc_el = content.select_one("p.text-2xs")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""
    titre = description or f"{type_bien.title()} {ville}".strip()

    # Photo
    photos = []
    for img in card.select("img.image, .image-container img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "notaires_berrynivernais",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",  # CP non exposé en liste ; dept fiable via slug+loc
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Notaires Berry-Nivernais",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Aubigny-sur-Nère - Cher (18)' → ('Aubigny-sur-Nère', '18')"""
    dept = ""
    m = re.search(r"\((\d{2,3})\)\s*$", text)
    if m:
        dept = m.group(1)[:2]
    ville = re.sub(r"\s*-\s*[^-]*\(\d{2,3}\)\s*$", "", text).strip()
    if not ville:
        ville = text.strip()
    return ville, dept


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    if v and v < 1000:  # garde-fou : ce n'était pas un prix
        return None
    return v


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_surface(text: str) -> float | None:
    """'84.56m2 - 7 pièces' → 84.56"""
    m = re.search(r"([\d]+(?:[.,]\d+)?)\s*m2", text, re.IGNORECASE)
    if m:
        try:
            f = float(m.group(1).replace(",", "."))
            if 8 <= f <= 5000:
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
    print(f"\nTotal Notaires Berry-Nivernais: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

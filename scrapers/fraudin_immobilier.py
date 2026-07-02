"""scrapers/fraudin_immobilier.py — Fraudin Immobilier (agence indépendante à Laval, 53)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress, thème custom)
URL : /biens/  → liste unique de tous les biens à la vente/location.
      Le site est mono-agence (Laval / Mayenne) : tout l'inventaire est en
      département 53. Pas de filtre serveur par département (inutile : tout est
      en 53), donc on POST-FILTRE strict sur code_postal[:2] == dept par sécurité.
      Le param ?categorie=3 (achat) existe mais isole mal les maisons → on
      scrape /biens/ et on filtre par type depuis le titre.

Cartes : a.property-preview
  - URL    : href de l'ancre  → /biens/{slug}/
  - Titre  : h2.property-preview-title
             format "Type - {surface} m² - {pieces} pièces - {Ville} {CP}"
             (surface/pièces optionnels ; ville + CP en fin de titre)
  - Prix   : .property-preview-price  → "204 750 €"
  - Photo  : figure picture img[src] / source[srcset]

Type / surface / pièces / ville / CP sont extraits du titre. Le terrain, le DPE
et la description complète ne sont pas dans la liste → enrichis ultérieurement
par scrapers/gallery.py sur la page détail.

Volume : petite agence (~5-7 biens, tous en 53). 0 fuite hors-département.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.fraudin-immobilier.fr"
LIST_URL = f"{BASE_URL}/biens/"
PHOTOS_PER_CARD = 5


# Types conservés (maisons / propriétés) — filtré depuis le 1er segment du titre.
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|longere|longère|manoir|chateau|château|"
    r"moulin|demeure|domaine|mas|ferme|corps de ferme|gite|gîte",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|fond|garage|parking|immeuble|bureau|"
    r"fonds|viager",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Le site n'est implanté que sur le 53 (Laval / Mayenne) : si 53 n'est pas
    # une cible, rien à récupérer.
    if "53" not in departements:
        print("[Fraudin] Dept 53 hors cibles — aucun inventaire pertinent.")
        return []

    results: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        try:
            r = await client.get(LIST_URL)
        except Exception as e:
            print(f"[Fraudin] Erreur requête : {e}")
            return []
        if r.status_code != 200:
            print(f"[Fraudin] HTTP {r.status_code} sur {LIST_URL}")
            return []

        cards = BeautifulSoup(r.text, "html.parser").select("a.property-preview")
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue

            # POST-FILTRE DÉPARTEMENT STRICT (0 fuite) : on n'accepte que les CP
            # appartenant aux départements cibles.
            cp = bien.get("code_postal") or ""
            if not cp or cp[:2] not in departements:
                continue
            bien["departement"] = cp[:2]

            aid = bien["url"]
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

        await asyncio.sleep(0.5)

    print(f"[Fraudin] {len(results)} annonces retenues (dept cible)")
    return results


def _parse_card(card) -> dict | None:
    href = card.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    title_el = card.select_one(".property-preview-title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        return None

    # "Type - {surface} m² - {pieces} pièces - {Ville} {CP}"
    parts = [p.strip() for p in titre.split(" - ") if p.strip()]
    type_seg = parts[0] if parts else ""
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg.lower()

    surface = None
    pieces = None
    for seg in parts[1:]:
        m = re.match(r"([\d.,]+)\s*m²", seg)
        if m:
            try:
                surface = float(m.group(1).replace(",", "."))
            except ValueError:
                pass
        m2 = re.match(r"(\d+)\s*pi[eè]ce", seg, re.IGNORECASE)
        if m2:
            pieces = int(m2.group(1))

    # Localisation : dernier segment "Ville 53000"
    loc = parts[-1] if parts else ""
    cp_m = re.search(r"(\d{5})", loc)
    code_postal = cp_m.group(1) if cp_m else ""
    ville = re.sub(r"\s*\d{5}\s*$", "", loc).strip()

    price_el = card.select_one(".property-preview-price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    photos = []
    for img in card.select("figure img, picture img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    # id_annonce : slug final de l'URL
    slug = [p for p in href.split("/") if p]
    id_annonce = slug[-1] if slug else url

    return {
        "source": "fraudin_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Fraudin Immobilier",
    }


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


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
    print(f"\nTotal Fraudin: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

"""scrapers/morvan_immobilier.py — Morvan Immobilier (agence locale Lormes, Nièvre)

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern :
  - Liste : /annonces  (TOUTES les annonces dans une seule page, ~25 cartes ;
    le paramètre ?page=N renvoie le même contenu → pas de vraie pagination).
  - Détail : /annonce/{ref}  (ref = id numérique de la carte).

Filtre département : agence MONO-ZONE (cœur du Morvan / Nièvre 58, déborde
  marginalement sur 21/89/71). PAS de filtre serveur par dept → on récupère
  tout puis POST-FILTRE strict sur code_postal[:2] ∈ départements cibles.
  0 fuite hors-zone.

Cartes : div.annonceCard
  - Titre/loc : h2.annonce-title  →  "à LORMES (58140)"  (ville + CP)
  - Prix      : h3.annoncesPrix   →  "39000 €"
  - Réf       : .annoncesRef      →  "Ref : 2565"  (+ checkbox id "checkbox2565")
  - Desc      : .annoncesDesc     (description tronquée)
  - Caracs    : div.carac × N     →  "surface de 100 m²", "4 pièces",
                                       "3 chambres", "terrain de 128 m²"
Détail (/annonce/{ref}) : h1 "Ref : NNNN - {Type} à {Ville} (CP)" → type de bien ;
  photos via /image/{hash}/photo_{hash}.jpg.

Types : on garde maison / propriété / longère / ferme / manoir / moulin / domaine ;
  on exclut appartement / terrain / immeuble / commerce / garage via le type
  déduit du titre de détail (ou du libellé de la carte).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://morvan-immobilier.com"
LIST_URL = f"{BASE_URL}/annonces"
PHOTOS_PER_BIEN = 10
CONCURRENCY = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|fermette|longere|longère|manoir|"
    r"chateau|château|moulin|demeure|domaine|gite|gîte|pavillon|corps de ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|immeuble|local|commerce|garage|parking|bureau|"
    r"fonds|etang|étang|grange",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        try:
            r = await client.get(LIST_URL)
            r.raise_for_status()
        except Exception as e:
            print(f"[MorvanImmo] Erreur liste: {e}")
            return results

        cards = BeautifulSoup(r.text, "html.parser").select(".annonceCard")
        print(f"[MorvanImmo] {len(cards)} cartes dans /annonces")

        parsed = []
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if bien:
                parsed.append(bien)

        # Pré-filtre dept + bornes AVANT d'aller chercher les détails (économie)
        retained = []
        for bien in parsed:
            cp = bien.get("code_postal") or ""
            if not cp or cp[:2] not in departements:
                continue
            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue
            retained.append(bien)

        print(f"[MorvanImmo] {len(retained)} cartes retenues (zone + bornes) "
              f"→ enrichissement détail")

        sem = asyncio.Semaphore(CONCURRENCY)

        async def _enrich(bien: dict) -> dict:
            async with sem:
                try:
                    await _fill_detail(client, bien)
                except Exception as e:
                    print(f"[MorvanImmo] détail {bien['id_annonce']}: {e}")
                await asyncio.sleep(0.4)
                return bien

        retained = await asyncio.gather(*[_enrich(b) for b in retained])

    # Filtre type APRÈS détail (le type fiable vient du titre de détail) + re-check dept
    for bien in retained:
        tb = bien.get("type_bien") or ""
        if _EXCLUDE_TYPE.search(tb) and not _KEEP_TYPE.search(tb):
            continue
        if not _KEEP_TYPE.search(tb):
            continue
        cp = bien.get("code_postal") or ""
        if not cp or cp[:2] not in departements:
            continue
        results.append(bien)

    print(f"[MorvanImmo] {len(results)} biens retenus (type + zone)")
    return results


def _parse_card(card) -> dict | None:
    title_el = card.select_one(".annonce-title")
    loc = title_el.get_text(" ", strip=True) if title_el else ""
    ville, code_postal = _parse_loc(loc)

    price_el = card.select_one(".annoncesPrix")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    ref = ""
    cb = card.select_one("input.checkbox")
    if cb and cb.get("id"):
        m = re.search(r"(\d+)", cb.get("id"))
        if m:
            ref = m.group(1)
    if not ref:
        ref_el = card.select_one(".annoncesRef")
        if ref_el:
            m = re.search(r"(\d+)", ref_el.get_text())
            if m:
                ref = m.group(1)
    if not ref:
        return None

    desc_el = card.select_one(".annoncesDesc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""
    description = re.sub(r"^Ref\s*:\s*\d+\s*", "", description).strip(' "')

    caracs = " ".join(c.get_text(" ", strip=True) for c in card.select(".carac"))
    surface = _first_int(r"surface de\s*([\d\s\xa0]+)\s*m", caracs)
    surface_terrain = _first_int(r"terrain de\s*([\d\s\xa0]+)\s*m", caracs)
    pieces = _first_int(r"([\d]+)\s*pi[eè]ces", caracs)
    chambres = _first_int(r"([\d]+)\s*chambres", caracs)

    img_el = card.select_one("img")
    photos = []
    if img_el:
        src = img_el.get("src") or img_el.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(_abs(src))

    return {
        "source": "morvan_immobilier",
        "url": f"{BASE_URL}/annonce/{ref}",
        "id_annonce": ref,
        "titre": (loc or ville)[:150],
        "type_bien": "",  # rempli au détail
        "description": description[:1200],
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
        "agence": "Morvan Immobilier",
    }


async def _fill_detail(client: httpx.AsyncClient, bien: dict) -> None:
    r = await client.get(bien["url"])
    if r.status_code != 200:
        return
    soup = BeautifulSoup(r.text, "html.parser")

    h1 = soup.select_one("h1")
    if h1:
        txt = h1.get_text(" ", strip=True)
        # "Ref : 2565 - Maison à Lormes (58140)"
        m = re.search(r"-\s*([A-Za-zÀ-ÿ' ]+?)\s+à\s", txt)
        type_bien = m.group(1).strip().lower() if m else ""
        bien["type_bien"] = type_bien or "maison"
        bien["titre"] = re.sub(r"^Ref\s*:\s*\d+\s*-\s*", "", txt)[:150]
    else:
        bien["type_bien"] = "maison"

    photos = []
    for ph in re.findall(r"/image/[a-f0-9]+/photo_[a-f0-9]+\.jpg", r.text):
        full = _abs(ph)
        if full not in photos:
            photos.append(full)
    if photos:
        bien["photos"] = photos[:PHOTOS_PER_BIEN]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _abs(src: str) -> str:
    if src.startswith("http"):
        return src
    if src.startswith("//"):
        return "https:" + src
    return BASE_URL + ("" if src.startswith("/") else "/") + src


def _parse_loc(text: str) -> tuple[str, str]:
    """'à LORMES (58140)' → ('Lormes', '58140')"""
    cp = ""
    m = re.search(r"\((\d{5})\)", text)
    if m:
        cp = m.group(1)
    ville = re.sub(r"\(\d{5}\)", "", text).strip()
    ville = re.sub(r"^à\s+", "", ville, flags=re.IGNORECASE).strip()
    return ville.title(), cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    if v is not None and v < 1000:
        return None
    return v


def _first_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        return int(val)
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
    print(f"\nTotal Morvan Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

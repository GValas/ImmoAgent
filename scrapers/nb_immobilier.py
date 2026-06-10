"""scrapers/nb_immobilier.py — NB Immobilier (agence locale, Laval / Mayenne 53)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress + plugin immobilier maison)
URL liste : /acheter/   → toutes les annonces de vente (SSR, pas de JS).
            Pas de filtre département serveur (agence mono-implantation Laval, 53) :
            on POST-FILTRE STRICTEMENT sur code_postal[:2] ∈ départements cibles.

Cartes liste : article.type-bien-vente
  - URL    : a[href]                       → /biens-immobiliers/{slug}/
  - Img    : img.bienimmo__tease__img[src]
  - Titre  : .bienimmo__tease__title
  - Loc    : .bienimmo__tease__localisation → "53000 Laval"
  - Prix   : .bienimmo__tease__prix         → "304 500,00 €"

Page détail (enrichissement, 1 requête/bien) :
  - Pictos : .bienimmo__picto__info  → "Nombre de pièce 5", "Surface 103 m2",
             "Surface du terrain 1300 m2", "Nombre de chambre(s) 3"…
  - Ref    : .bienimmo__ref           → "Référence de l'annonce : N° 2466"
  - Type   : .bienimmo__tags          → "Vente | Maison/Villa" | "Appartement"
  - Desc   : .bienimmo__desc
  - DPE    : .diagnostic-diagram.number{X}  → lettre A..G active
  - Photos : .bienimmo__slider img / .bienimmo__diapo img

Couverture : agence implantée à Laval → stock 53 (Mayenne, cible) uniquement.
             Petit inventaire (~5 biens), mais réel. Le post-filtre CP[:2] garantit
             0 fuite hors-zone si l'agence venait à publier hors 53.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://nbimmobilier.fr"
LIST_URL = f"{BASE_URL}/acheter/"
PHOTOS_PER_BIEN = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien conservés (déduits du titre / tag). On garde maisons/propriétés.
_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|gite|g[iî]te|corps de ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_urls: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        try:
            r = await client.get(LIST_URL)
        except Exception as e:
            print(f"[NBImmobilier] Erreur liste: {e}")
            return results
        if r.status_code != 200:
            print(f"[NBImmobilier] Liste status {r.status_code}")
            return results

        cards = BeautifulSoup(r.text, "html.parser").select("article.type-bien-vente")
        print(f"[NBImmobilier] {len(cards)} annonces en liste")

        for card in cards:
            base = _parse_card(card)
            if not base:
                continue
            url = base["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # Post-filtre département STRICT (aucun filtre serveur) — 0 fuite.
            cp = base.get("code_postal") or ""
            if not cp or cp[:2] not in departements:
                continue

            # Enrichissement page détail (surface, pièces, terrain, dpe, photos…)
            try:
                bien = await _enrich(client, base)
            except Exception as e:
                print(f"[NBImmobilier] Détail KO {url}: {e}")
                bien = base

            # Re-vérification département après détail (la loc peut être réécrite).
            cp2 = bien.get("code_postal") or ""
            if not cp2 or cp2[:2] not in departements:
                continue

            # Exclure les types non désirés (studio/appartement/terrain…).
            tb = (bien.get("type_bien") or "") + " " + (bien.get("titre") or "")
            if _EXCLUDE_TYPE.search(tb) and not _KEEP_TYPE.search(tb):
                continue

            # Bornes prix / surface (un champ manquant n'exclut pas).
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

    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    title_el = card.select_one(".bienimmo__tease__title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    loc_el = card.select_one(".bienimmo__tease__localisation")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)

    price_el = card.select_one(".bienimmo__tease__prix")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    img_el = card.select_one("img.bienimmo__tease__img")
    photos = []
    if img_el:
        src = img_el.get("src") or img_el.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(src)

    # id_annonce : id du post (article id="post-2577") en secours
    aid = card.get("id", "").replace("post-", "") or url

    # Surface depuis le titre si présente (ex. "Maison de ville 80 m2 LAVAL")
    surface = _parse_surface_from_text(titre)

    return {
        "source": "nb_immobilier",
        "url": url,
        "id_annonce": aid,
        "titre": titre[:150],
        "type_bien": _guess_type(titre),
        "description": "",
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "NB Immobilier",
    }


async def _enrich(client: httpx.AsyncClient, base: dict) -> dict:
    r = await client.get(base["url"])
    if r.status_code != 200:
        return base
    ds = BeautifulSoup(r.text, "html.parser")

    # Pictos (caractéristiques)
    picto_text = " | ".join(
        p.get_text(" ", strip=True) for p in ds.select(".bienimmo__picto__info")
    )
    pieces = _parse_int(r"Nombre de pi[eè]ces?\s*(\d+)", picto_text)
    chambres = _parse_int(r"Nombre de chambre\(?s?\)?\s*(\d+)", picto_text)
    surface = _parse_picto_surface(r"Surface\s+(\d[\d\s]*)\s*m", picto_text)
    surface_terrain = _parse_picto_surface(
        r"Surface du terrain\s+(\d[\d\s]*)\s*m", picto_text
    )

    # Type via tags
    tags_el = ds.select_one(".bienimmo__tags")
    tags_txt = tags_el.get_text(" ", strip=True) if tags_el else ""
    type_bien = _guess_type(tags_txt) or base["type_bien"]

    # Référence
    ref_el = ds.select_one(".bienimmo__ref")
    if ref_el:
        m = re.search(r"N[°o]\s*(\w+)", ref_el.get_text(" ", strip=True))
        if m:
            base["id_annonce"] = m.group(1)

    # Description
    desc_el = ds.select_one(".bienimmo__desc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Loc (page détail) — re-confirme CP
    loc_el = ds.select_one(".bienimmo__localisation")
    if loc_el:
        ville, cp = _parse_loc(loc_el.get_text(" ", strip=True))
        if cp:
            base["ville"] = ville[:80]
            base["code_postal"] = cp
            base["departement"] = cp[:2]

    # Prix (page détail) en secours
    if not base.get("prix"):
        price_el = ds.select_one(".bienimmo__price")
        if price_el:
            base["prix"] = _parse_price(price_el.get_text(" ", strip=True))

    # DPE : classe diagnostic-diagram number{X}
    dpe = _parse_dpe(ds)

    # Photos galerie
    photos = list(base.get("photos") or [])
    for img in ds.select(".bienimmo__slider img, .bienimmo__diapo img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:") and src not in photos:
            photos.append(src)
    photos = photos[:PHOTOS_PER_BIEN]

    base.update(
        {
            "type_bien": type_bien,
            "description": description[:1200],
            "surface": surface or base.get("surface"),
            "surface_terrain": surface_terrain,
            "pieces": pieces,
            "chambres": chambres,
            "photos": photos,
            "dpe": dpe,
        }
    )
    return base


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'53000 Laval' → ('Laval', '53000')"""
    cp = ""
    m = re.search(r"\b(\d{5})\b", text)
    if m:
        cp = m.group(1)
    ville = re.sub(r"\b\d{5}\b", "", text).strip()
    ville = re.sub(r"\s{2,}", " ", ville).strip(" -|")
    return ville, cp


def _parse_price(text: str) -> float | None:
    # "304 500,00 €" → 304500.0
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = cleaned.replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)", cleaned)
    if not m:
        return None
    try:
        return round(float(m.group(1)))
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_picto_surface(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
        return f if 1 <= f <= 100000 else None
    except ValueError:
        return None


def _parse_surface_from_text(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d[\d\s]*(?:[.,]\d+)?)\s*m2|\s*m²", text, re.IGNORECASE)
    if not m or not m.group(1):
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
    try:
        f = float(val)
        return f if 8 <= f <= 2000 else None
    except ValueError:
        return None


def _guess_type(text: str) -> str:
    if not text:
        return "maison"
    if _KEEP_TYPE.search(text):
        m = _KEEP_TYPE.search(text)
        return m.group(0).lower()
    if re.search(r"appartement", text, re.IGNORECASE):
        return "appartement"
    if re.search(r"studio", text, re.IGNORECASE):
        return "studio"
    if re.search(r"terrain", text, re.IGNORECASE):
        return "terrain"
    return "maison"


def _parse_dpe(ds) -> str | None:
    diagram = ds.select_one(".diagnostic-diagram")
    if not diagram:
        return None
    classes = " ".join(diagram.get("class", []))
    m = re.search(r"number([A-G])", classes)
    if m:
        return m.group(1).upper()
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
    print(f"\nTotal NB Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']} — DPE {b.get('dpe')}"
        )

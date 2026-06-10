"""scrapers/decizeimmo.py — Decize Immobilier (agence indépendante, Decize / Nièvre 58)

Méthode : scrape_simple (httpx) — SSR HTML, CMS La Boite Immo (staticlbi.com).

Couverture : agence mono-secteur centrée sur Decize (58) et le sud-Nièvre
             (Saint-Léger-des-Vignes, Châtillon-en-Bazois, Cercy-la-Tour…).
             Quelques biens en limitrophe Saône-et-Loire (71140) → post-filtrés.
             AUCUN slug département en URL : la liste /vente/{page} renvoie TOUT
             l'inventaire → on post-filtre strictement sur code_postal[:2].

URL pattern :
  - Liste   : /vente/{page}   (page 1.. ; ~10 cartes/page, ~52 biens au total).
              S'arrête dès qu'une page ne contient plus de cartes.
  - Détail  : href de la carte → /vente/{NN-ville}/{type}/{idbien-slug}

Cartes : article.card_bien__structure
  - URL    : a.card_bien__link[href]
  - Titre  : .card_bien__title  → "Maison 9 pièce(s) 5 chambre(s) 140 m²"
             (type + pièces + chambres + surface habitable dans le même texte)
  - Loc    : .card_bien__localisation  → "Ville (CODEPOSTAL)"
  - Prix   : .card_bien__prix  → "157 000 €"
  - Réf    : idbien dans button[data-add-url="/i/selection/addbien?idbien=NNNN"]
             ou id numérique du slug final de l'URL
  - Photos : picture.swiper-picture source[srcset] / img.swiper-img[src]
             (//decizeimmo.staticlbi.com/...  → https:)

Type de bien : 1er mot du titre (Maison, Immeuble, Garage, Appartement…).
               On exclut appartement / garage / commerce / local / parking…

Post-filtre dept STRICT : bien["code_postal"][:2] in departements → 0 fuite
                          (le 71140 limitrophe est ainsi écarté hors-zone).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.decizeimmo.com"
MAX_PAGES = 12
PHOTOS_PER_CARD = 10

# Département(s) réellement couvert(s) par l'agence (Nièvre). Optimisation :
# si la zone demandée ne recoupe pas cette zone, inutile de scraper.
COVERED_DEPTS = {"58"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_EXCLUDE_TYPE = re.compile(
    r"appartement|garage|parking|local|commerce|bureau|fonds|terrain",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    # L'agence ne couvre que la Nièvre (58). Si la zone demandée ne recoupe pas
    # cette couverture, il n'y a rien à trouver — on évite de scraper.
    if not (set(departements) & COVERED_DEPTS):
        print(
            f"[Decizeimmo] Zone demandée {departements} hors couverture "
            f"{sorted(COVERED_DEPTS)} → 0 annonce (skip)."
        )
        return []

    raw: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/vente/{page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Decizeimmo] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                "article.card_bien__structure"
            )
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien or bien["id_annonce"] in seen_ids:
                    continue
                seen_ids.add(bien["id_annonce"])
                raw.append(bien)
                new_on_page += 1

            if new_on_page == 0:
                break
            await asyncio.sleep(0.5)

    print(f"[Decizeimmo] {len(raw)} annonces brutes récupérées.")

    # Filtrage final : dept STRICT + bornes prix / surface.
    out: list[dict] = []
    vus_hors = set()
    for b in raw:
        cp = b.get("code_postal") or ""
        if not cp:
            continue  # sans CP, dept non garanti → on écarte
        dept = cp[:2]
        if dept not in departements:
            vus_hors.add(dept)
            continue
        b["departement"] = dept

        p = b.get("prix") or 0
        s = b.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue
        out.append(b)

    if vus_hors:
        print(f"[Decizeimmo] Écartés hors zone (depts {sorted(vus_hors)}).")
    print(f"[Decizeimmo] {len(out)} annonces retenues pour {departements}.")
    return out


# ── Parsing carte ─────────────────────────────────────────────────────────────

def _parse_card(card) -> dict | None:
    link = card.select_one("a.card_bien__link")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce : idbien du bouton sélection, sinon id numérique du slug final.
    id_annonce = ""
    btn = card.select_one("button[data-add-url]")
    if btn:
        m = re.search(r"idbien=(\d+)", btn.get("data-add-url", ""))
        if m:
            id_annonce = m.group(1)
    if not id_annonce:
        m = re.search(r"/(\d+)-", href)
        if m:
            id_annonce = m.group(1)
    id_annonce = id_annonce or url

    # Titre : "Maison 9 pièce(s) 5 chambre(s) 140 m²"
    title_el = card.select_one(".card_bien__title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    titre = re.sub(r"\s+", " ", titre).strip()

    type_bien, pieces, chambres, surface = _parse_title(titre)
    if type_bien and _EXCLUDE_TYPE.search(type_bien):
        return None

    # Localisation : "Ville (58300)"
    loc_el = card.select_one(".card_bien__localisation")
    loc_txt = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc_txt)

    # Prix : "157 000 €"
    price_el = card.select_one(".card_bien__prix")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Photos
    photos: list[str] = []
    for src in card.select("picture.swiper-picture source"):
        ss = src.get("srcset", "")
        if ss:
            photos.append(_abs_img(ss.split()[0]))
    for img in card.select("img.swiper-img"):
        s = img.get("src")
        if s:
            photos.append(_abs_img(s))
    photos = [p for p in dict.fromkeys(photos) if p and not p.startswith("data:")]
    photos = photos[:PHOTOS_PER_CARD]

    if not titre:
        titre = f"{type_bien or 'Bien'} {ville}".strip()

    return {
        "source": "decizeimmo",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": (type_bien or "maison").lower(),
        "description": "",
        "departement": None,  # rempli au filtrage final
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Decize Immobilier",
    }


def _parse_title(text: str) -> tuple[str, int | None, int | None, float | None]:
    """'Maison 9 pièce(s) 5 chambre(s) 140 m²' → (type, pieces, chambres, surface)."""
    type_bien = ""
    pieces = None
    chambres = None
    surface = None
    if not text:
        return type_bien, pieces, chambres, surface

    m_type = re.match(r"\s*([A-Za-zÀ-ÿ'’\- ]+?)(?=\s*\d|\s*$)", text)
    if m_type:
        type_bien = m_type.group(1).strip()

    m_p = re.search(r"(\d+)\s*pi[eè]ce", text, re.IGNORECASE)
    if m_p:
        pieces = int(m_p.group(1))

    m_c = re.search(r"(\d+)\s*chambre", text, re.IGNORECASE)
    if m_c:
        chambres = int(m_c.group(1))

    m_s = re.search(r"([\d]+(?:[.,]\d+)?)\s*m²", text)
    if m_s:
        try:
            f = float(m_s.group(1).replace(",", "."))
            if 5 <= f <= 5000:
                surface = f
        except ValueError:
            pass

    return type_bien, pieces, chambres, surface


def _parse_loc(text: str) -> tuple[str, str]:
    """'Châtillon-en-Bazois (58110)' → ('Châtillon-en-Bazois', '58110')."""
    cp = ""
    m = re.search(r"\((\d{5})\)", text)
    if m:
        cp = m.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


# ── Helpers ─────────────────────────────────────────────────────────────────

def _abs_img(src: str) -> str:
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("http"):
        return src
    return BASE_URL + src


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text or "")
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
    print(f"\nTotal Decize Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

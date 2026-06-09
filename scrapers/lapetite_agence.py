"""scrapers/lapetite_agence.py — La Petite Agence (réseau Centre / Berry, siège Vierzon)

Méthode : scrape_simple (httpx) — SSR HTML (plateforme la-boite-immo)
URL pattern : /vente/{NN-dept-slug}/{page}   (ex: /vente/18-cher/1)
              → filtre département CÔTÉ SERVEUR (vérifié : aucune fuite hors-dept).
              Pagination en fin d'URL (/1, /2, ...). Filtre ville optionnel non utilisé.

Cartes : div.property-listing-v2__item  (10 par page)
  - URL    : .item__global-link[href] ou a.item__link[href]
             → /vente/{NN-dept}/{ville}/{type}/{tN}/{id-slug}/
  - Loc    : .title-subtitle__subtitle  →  "Ville (CODEPOSTAL)"
  - Titre  : .title-subtitle__content
  - Surf+prix: .item__info-extra  →  "142 m²" puis .__price-value "149 000 €"
  - Options: .item__info-options .option  (paires nombre/label :
             "Pièce(s)", "m²" = terrain, "Chambre(s)")
  - Photos : img.item__img[data-src]  (// → https:)

Type de bien : déduit du segment d'URL (1-maison, 5-terrain, 45-terrain-de-loisir...).
               On ne garde que maisons / propriétés / demeures.

Couverture : réseau Centre / Berry. Stock réel sur 18 (cher, ~150 annonces) et
             marginal sur 36 (indre). Les départements 72/28/45/89/58/41 sont à 0
             (hors implantation). Scraper fonctionnel et 0 fuite vérifié sur le 18.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.lapetite-agence.com"
MAX_PAGES = 20
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Code département → slug URL /vente/{NN-slug}/{page}
DEPT_SLUGS: dict[str, str] = {
    "72": "sarthe",
    "28": "eure-et-loir",
    "45": "loiret",
    "89": "yonne",
    "49": "maine-et-loire",
    "37": "indre-et-loire",
    "36": "indre",
    "18": "cher",
    "58": "nievre",
    "41": "loir-et-cher",
    "53": "mayenne",
}

# Types de bien (segment d'URL) à conserver : maisons / propriétés / fermes...
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|maison-de-village",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for dept in departements:
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, slug, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[LaPetiteAgence] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[LaPetiteAgence] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    slug: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/vente/{dept}-{slug}/{page}"
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select(
            "div.property-listing-v2__item"
        )
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            try:
                bien = _parse_card(card, dept)
            except Exception:
                continue
            if not bien:
                continue

            # Sécurité : on n'accepte que le département cible (filtre serveur déjà OK)
            if bien["code_postal"] and bien["code_postal"][:2] != dept:
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
            biens.append(bien)
            new_on_page += 1

        if new_on_page == 0:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one(".item__global-link") or card.select_one("a.item__link")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL : /vente/18-cher/{ville}/{type}/{tN}/{id-slug}/
    parts = [p for p in href.split("/") if p]
    type_seg = parts[3] if len(parts) > 3 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean) and not _KEEP_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        # type inconnu/ambigu → on exclut par prudence
        return None
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # id_annonce : préfixe numérique du slug final
    id_num = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_num = m.group(1)
    id_annonce = id_num or url

    # Localisation : "Ville (CODEPOSTAL)"
    sub_el = card.select_one(".title-subtitle__subtitle")
    loc = sub_el.get_text(" ", strip=True) if sub_el else ""
    ville, code_postal = _parse_loc(loc)

    # Titre
    title_el = card.select_one(".title-subtitle__content")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Prix
    price_el = card.select_one(".__price-value")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Surface habitable : premier ".item__info-extra" feuille contenant "m²"
    surface = None
    for ex in card.select(".item__info-extra"):
        if ex.select(".item__info-extra"):
            continue  # conteneur, pas une feuille
        txt = ex.get_text(" ", strip=True)
        if "€" in txt:
            continue
        m = re.search(r"([\d\s\xa0]+)\s*m", txt)
        if m:
            val = re.sub(r"[\s\xa0]", "", m.group(1))
            try:
                f = float(val)
                if 8 <= f <= 2000:
                    surface = f
                    break
            except ValueError:
                pass

    # Options : paires (nombre, label) → Pièce(s), m² (terrain), Chambre(s)
    pieces = chambres = None
    surface_terrain = None
    for opt in card.select(".item__info-options .option"):
        num_el = opt.select_one(".option__number")
        lab_el = opt.select_one(".option__label")
        if not num_el or not lab_el:
            continue
        num_txt = re.sub(r"[\s\xa0]", "", num_el.get_text(strip=True))
        label = lab_el.get_text(strip=True).lower()
        try:
            num = float(num_txt)
        except ValueError:
            continue
        if "pièce" in label or "piece" in label:
            pieces = int(num)
        elif "chambre" in label:
            chambres = int(num)
        elif "m²" in label or "m2" in label:
            surface_terrain = num

    # Pièces en secours : segment tN de l'URL
    if pieces is None and len(parts) > 4:
        m = re.match(r"^t(\d+)$", parts[4])
        if m:
            pieces = int(m.group(1))

    # Photos
    photos = []
    for img in card.select("img.item__img"):
        src = img.get("data-src") or img.get("data-lazy") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "lapetite_agence",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "La Petite Agence",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Dun-sur-Auron (18130)' → ('Dun-sur-Auron', '18130')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text).replace(",", ".")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
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
    print(f"\nTotal La Petite Agence: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

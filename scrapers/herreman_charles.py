"""scrapers/herreman_charles.py — Herreman & Charles (agence de charme Gers / Gascogne)

Méthode : scrape_simple (httpx) — SSR HTML (Apache, pas de Cloudflare).
URL pattern : /vente/{NN-dept-slug}/{page}   (ex: /vente/32-gers/1)
              → filtre département CÔTÉ SERVEUR (vérifié : un dept hors-zone
                renvoie 0 carte, pas de repli national → aucune fuite).
              Template d'URL identique à Le Tuc (même CMS LBI/staticlbi).

Cartes : div.property-listing-v3__item
  - URL   : a.links-group__link[href]  → /vente/{NN-dept}/{ville}/{type}/{tN}/{id-slug}/
  - Titre : .title-subtitle__content (h2)
  - Loc   : .title-subtitle__subtitle  →  "Ville (CODEPOSTAL)"
  - Extra : .item__info-extra  →  "240 m²" et "457 000 €" (séparés)
  - Réf   : .item__info-id  →  "Réf : 2236"
  - Photos: img[data-src]  (URL protocol-relative //...staticlbi.com/...)

Type de bien : déduit du segment d'URL (1-maison, t-propriete, terrain...).
               On ne garde que maisons / propriétés / biens de caractère.
Pièces : segment tN de l'URL.

Couverture : agence locale Gers (32, ~92 biens) et Tarn-et-Garonne (82) / Gascogne.
             AUCUN des départements cibles actuels (72/28/45/89) n'est couvert →
             retournera 0 bien sur la zone par défaut, mais le scraper est
             fonctionnel (filtre serveur + post-filtre CP[:2] OK, 0 fuite).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.herreman-charles.com"
MAX_PAGES = 12
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Code département → slug URL herreman-charles.com/vente/{NN-slug}/{page}
# Le filtre serveur accepte n'importe quel slug normalisé ; les départements
# cibles actuels (72/28/45/89) renvoient simplement 0 carte (hors implantation).
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
    # Implantation réelle de l'agence (au cas où la zone évolue) :
    "32": "gers",
    "82": "tarn-et-garonne",
}

# Types de bien (segment d'URL) à conserver
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|"
    r"maison-de-village|fermette|grange|batisse|bâtisse",
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
                print(f"[HerremanCharles] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[HerremanCharles] Erreur dept {dept}: {e}")
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
            "div.property-listing-v3__item"
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

            # Post-filtre STRICT : on n'accepte que le département cible.
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
    link = card.select_one("a.links-group__link") or card.find("a", href=True)
    href = link.get("href", "") if link else ""
    if not href or "/vente/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL :
    # /vente/{NN-dept}/{ville}/{type}/{tN}/{id-slug}/
    parts = [p for p in href.split("/") if p]
    type_seg = parts[3] if len(parts) > 3 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean) and not _KEEP_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        return None
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # Référence (id_annonce)
    ref_el = card.select_one(".item__info-id")
    ref_txt = ref_el.get_text(" ", strip=True) if ref_el else ""
    m_ref = re.search(r"R[ée]f\s*:?\s*([A-Za-z0-9\-]+)", ref_txt)
    ref = m_ref.group(1) if m_ref else ""
    id_num = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_num = m.group(1)
    id_annonce = ref or id_num or url

    # Localisation : "Ville (CODEPOSTAL)"
    sub_el = card.select_one(".title-subtitle__subtitle") or card.select_one(
        ".title-subtitle"
    )
    loc = sub_el.get_text(" ", strip=True) if sub_el else ""
    ville, code_postal = _parse_loc(loc)

    # Titre
    title_el = card.select_one(".title-subtitle__content")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Extras : surface (m²) et prix (€). Le 1er .item__info-extra regroupe
    # souvent les deux ("240 m² - 457 000 €") ; on ne lit donc QUE les éléments
    # atomiques (un seul m² OU un seul €) pour éviter de coller surface+prix.
    surface = None
    prix = None
    for el in card.select(".item__info-extra"):
        t = el.get_text(" ", strip=True)
        has_eur = "€" in t
        has_m2 = "m²" in t
        # Élément combiné (surface ET prix) → ignoré, on prend les atomiques
        if has_eur and has_m2:
            continue
        if has_eur and prix is None:
            prix = _parse_price(t)
        elif has_m2 and surface is None:
            ms = re.search(r"(\d[\d\s\xa0]*)\s*m²", t)
            if ms:
                val = re.sub(r"[\s\xa0]", "", ms.group(1))
                try:
                    f = float(val)
                    if 8 <= f <= 5000:
                        surface = f
                except ValueError:
                    pass
    # Prix de secours : valeur dédiée
    if prix is None:
        pv = card.select_one(".__price-value")
        if pv:
            prix = _parse_price(pv.get_text(" ", strip=True))
    # Surface de secours : extraire depuis l'élément combiné si besoin
    if surface is None:
        for el in card.select(".item__info-extra"):
            ms = re.search(r"(\d[\d\s\xa0]*)\s*m²", el.get_text(" ", strip=True))
            if ms:
                val = re.sub(r"[\s\xa0]", "", ms.group(1))
                try:
                    f = float(val)
                    if 8 <= f <= 5000:
                        surface = f
                        break
                except ValueError:
                    pass

    # Pièces : segment tN de l'URL
    pieces = None
    if len(parts) > 4:
        m = re.match(r"^t(\d+)$", parts[4])
        if m:
            pieces = int(m.group(1))

    # Photos
    photos = []
    for img in card.find_all("img"):
        src = img.get("data-src") or img.get("data-lazy") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "herreman_charles",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": None,
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Herreman & Charles",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Marciac (32230)' → ('Marciac', '32230')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
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
    print(f"\nTotal Herreman & Charles: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

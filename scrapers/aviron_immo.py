"""scrapers/aviron_immo.py — Aviron Conseil Immobilier (agence chartraine, Eure-et-Loir 28)

Méthode : scrape_simple (httpx) — SSR HTML (moteur LBI / staticlbi.com).
URL pattern : /vente/{NN-dept-slug}/{page}   (ex: /vente/28-eure-et-loir/1)
              → filtre département CÔTÉ SERVEUR (vérifié : aucune fuite hors-dept ;
              les depts sans stock renvoient 0 carte).

Cartes : div.property-listing-v3__item
  - URL/titre : a[href] (lien détail .../{id}-{slug}/) + attr title
  - Loc   : .title-subtitle__subtitle  →  "Ville <br> (CODEPOSTAL)"
  - Titre : .title-subtitle__content
  - Extra : .item__info-extra  →  "260 m²" (surface habitable) et prix
  - Prix  : .__price-value  →  "799 000 €"
  - Texte : .item__text-block (description tronquée)
  - Réf   : .item__info-id  →  "Réf : 620"
  - Photo : img.item__img[data-src]  (préfixe //aviron-immo.staticlbi.com/...)

Type de bien & pièces : déduits des segments d'URL
  /vente/28-.../10-luisant/1-maison/t8/5165-slug/  → type=maison, pieces=8.
On ne garde que maisons / propriétés / manoirs / longères (exclut appart/terrain).

Couverture : agence LOCALE de Chartres → quasi tout en dept 28 (~60 biens).
Les autres départements cibles renvoient 0 (pas d'implantation). Conservé pour
le dept 28 (Eure-et-Loir), couronne directe de la Sarthe.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.aviron-immo.fr"
MAX_PAGES = 15
PHOTOS_PER_CARD = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Code département → slug URL aviron-immo.fr/vente/{NN-slug}/{page}
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

# Types (segment d'URL) à conserver : maisons / propriétés / fermes...
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
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
                print(f"[AvironImmo] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[AvironImmo] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.5)

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

            # Sécurité 0-fuite : on n'accepte que le département cible.
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

        if new_on_page == 0 and page > 1:
            break
        # Moins d'une page pleine → dernière page
        if len(cards) < 10:
            break

        await asyncio.sleep(0.4)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a[href*='/vente/']")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type & pièces depuis l'URL : /vente/{NN-dept}/{ville}/{N-type}/tN/{id-slug}/
    # Le segment de type est celui juste AVANT le segment "tN" (nb de pièces).
    parts = [p for p in href.split("/") if p]
    type_seg = ""
    type_idx = -1
    pieces = None
    for i, seg in enumerate(parts):
        m_t = re.match(r"^t(\d+)$", seg)
        if m_t and i > 0:
            pieces = int(m_t.group(1))
            type_idx = i - 1
            type_seg = re.sub(r"^\d+-", "", parts[type_idx])
            break
    # Repli : pas de segment tN → on prend un segment "N-{type}" reconnu
    if not type_seg:
        for seg in parts:
            cand = re.sub(r"^\d+-", "", seg)
            if re.match(r"^\d+-", seg) and (
                _KEEP_TYPE.fullmatch(cand) or _EXCLUDE_TYPE.fullmatch(cand)
            ):
                type_seg = cand
                break

    if _EXCLUDE_TYPE.fullmatch(type_seg) and not _KEEP_TYPE.fullmatch(type_seg):
        return None
    if not _KEEP_TYPE.fullmatch(type_seg):
        return None
    type_bien = type_seg.replace("-", " ").strip() or "maison"

    # id annonce : entier en tête du dernier segment slug + réf affichée
    id_num = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_num = m.group(1)
    ref_el = card.select_one(".item__info-id")
    ref = ""
    if ref_el:
        m_ref = re.search(r"(\d+)", ref_el.get_text())
        if m_ref:
            ref = m_ref.group(1)
    id_annonce = id_num or ref or url

    # Localisation : "Luisant (28600)"
    sub_el = card.select_one(".title-subtitle__subtitle")
    loc = sub_el.get_text(" ", strip=True) if sub_el else ""
    ville, code_postal = _parse_loc(loc)

    # Titre
    title_el = card.select_one(".title-subtitle__content")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = (link.get("title", "") or "").replace("Voir le bien - ", "").strip()
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Description
    text_el = card.select_one(".item__text-block")
    description = text_el.get_text(" ", strip=True) if text_el else ""

    # Prix
    price_el = card.select_one(".__price-value")
    prix = _parse_num(price_el.get_text(" ", strip=True)) if price_el else None

    # Surface habitable : "260 m²" dans .item__info-extra (≠ celui du prix)
    surface = None
    for ex in card.select(".item__info-extra"):
        txt = ex.get_text(" ", strip=True)
        m = re.search(r"(\d[\d\s\xa0]*)\s*m²", txt)
        if m and "€" not in txt:
            val = re.sub(r"[\s\xa0]", "", m.group(1))
            try:
                f = float(val)
                if 8 <= f <= 3000:
                    surface = f
                    break
            except ValueError:
                pass

    # Photo
    photos = []
    img = card.select_one("img.item__img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "aviron_immo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
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
        "agence": "Aviron Conseil Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Luisant (28600)' → ('Luisant', '28600')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_num(text: str) -> float | None:
    """'799 000 €' → 799000.0"""
    cleaned = re.sub(r"[^\d]", "", text.replace("\xa0", " "))
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
    print(f"\nTotal Aviron Immo: {len(biens)} annonces")
    by_dept: dict[str, int] = {}
    for b in biens:
        d = (b["code_postal"] or "")[:2]
        by_dept[d] = by_dept.get(d, 0) + 1
    print(f"Par département : {dict(sorted(by_dept.items()))}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

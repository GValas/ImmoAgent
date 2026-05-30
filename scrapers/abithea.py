"""scrapers/abithea.py — Abithéa (réseau national d'agences)

Méthode : scrape_simple (httpx) — SSR PHP (moteur "property-listing-v3", LBI/staticlbi).
URL pattern : /vente/{NN-dept-slug}/{page}   (ex: /vente/94-val-de-marne/2)
              → filtre département CÔTÉ SERVEUR (vérifié : aucune fuite hors-dept ;
              chaque carte porte le segment {NN-dept} dans l'URL de la fiche).

Cartes : div.property-listing-v3__item.item
  - URL/fiche : a[href*='/vente/']  → /vente/{NN-dept}/{ville-id}/{type}/{tN}/{id-slug}/
  - Loc      : span.title-subtitle__subtitle  →  "Ville <br> (CODEPOSTAL)"
  - Titre    : h2.title-subtitle__content
  - Surface+prix : div.item__info-extra  →  "134,50 m²" et span.__price-value "599 000 €"
  - Texte    : .item__text-block (description courte)
  - Réf      : .item__info-id  →  "Réf : Mais-SavLH-2267"
  - id num   : segment final {id-slug} de l'URL + data-*-url ?idbien=NNNN
  - Photos   : img.item__img[data-src]  (//abithea.staticlbi.com/...)

Type de bien : déduit du segment d'URL ({1-maison, 2-appartement, ...}). On ne garde
               que maisons / propriétés / longères / manoirs (exclut appartements,
               terrains, commerces, parkings).

COUVERTURE (testé 2026-05-30) : réseau implanté principalement dans le Nord (62, 59),
Charente/Charente-Maritime (16, 17), Gironde (33), Hérault (34), Val-de-Marne (94),
Aube (10)... AUCUN des départements cibles (72/28/45/89/49/37/36/18/58/41/53) n'a
de stock. Les pages /vente/{NN-slug}/ de ces départements renvoient un listing VIDE
(0 carte) — non pas du JS manquant, mais une absence réelle d'inventaire.
→ Scraper INACTIF (actif: false) tant que la zone n'est pas couverte. Le code reste
  fonctionnel : il suffira de le réactiver si Abithéa s'implante dans la zone.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.abithea.fr"
MAX_PAGES = 10
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Code département → slug URL abithea.fr/vente/{NN-slug}/{page}
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
                print(f"[Abithea] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Abithea] Erreur dept {dept}: {e}")
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
            "div.property-listing-v3__item.item"
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

            # Sécurité anti-fuite : on n'accepte que le département cible
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
    link = card.select_one("a[href*='/vente/']")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL :
    # /vente/{NN-dept}/{ville-id}/{type}/{tN}/{id-slug}/
    parts = [p for p in href.split("/") if p]
    type_seg = parts[3] if len(parts) > 3 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean) and not _KEEP_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        return None
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # Localisation : "Ville (CODEPOSTAL)"
    sub_el = card.select_one(".title-subtitle__subtitle")
    loc = sub_el.get_text(" ", strip=True) if sub_el else ""
    ville, code_postal = _parse_loc(loc)

    # Titre
    title_el = card.select_one("h2.title-subtitle__content")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Description
    text_el = card.select_one(".item__text-block")
    description = text_el.get_text(" ", strip=True) if text_el else ""

    # Surface + prix : dans .item__info-extra ; prix dans span.__price-value
    price_el = card.select_one(".__price-value")
    prix = _parse_num(price_el.get_text(" ", strip=True)) if price_el else None

    # surface : le .item__info-extra dont le texte propre contient "m²" et qui
    # ne contient PAS le prix (les conteneurs sont imbriqués : surface, prix, et
    # un wrapper "surface - prix" partagent la classe .item__info-extra).
    surface = None
    for ex in card.select(".item__info-extra"):
        if ex.select_one(".__price-value"):
            continue
        own = "".join(ex.find_all(string=True, recursive=False)).strip()
        if "m²" in own or "m2" in own:
            surface = _parse_num(own)
            if surface:
                break

    # Pièces : segment tN de l'URL (/t6/)
    pieces = None
    if len(parts) > 4:
        m = re.match(r"^t(\d+)$", parts[4])
        if m:
            pieces = int(m.group(1))

    # Référence / id annonce
    ref_el = card.select_one(".item__info-id")
    ref = ""
    if ref_el:
        ref = re.sub(r"^R[ée]f\s*:\s*", "", ref_el.get_text(" ", strip=True)).strip()
    # id numérique : ?idbien=NNNN sur le bouton sélection, sinon segment final
    id_num = ""
    btn = card.select_one("[data-add-url]")
    if btn:
        m = re.search(r"idbien=(\d+)", btn.get("data-add-url", ""))
        if m:
            id_num = m.group(1)
    if not id_num and parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_num = m.group(1)
    id_annonce = id_num or ref or url

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
        "source": "abithea",
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
        "agence": "Abithéa",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Boissy-Saint-Léger (94470)' → ('Boissy-Saint-Léger', '94470')"""
    cp = ""
    m_cp = re.search(r"\(?(\d{5})\)?", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(?\d{5}\)?\s*$", "", text).strip()
    return ville, cp


def _parse_num(text: str) -> float | None:
    """'599 000 €' → 599000.0 ; '134,50 m²' → 134.5"""
    cleaned = text.replace("\xa0", " ")
    # supprime symboles unités, garde chiffres, espaces, virgule, point
    cleaned = re.sub(r"[^\d,\.\s]", "", cleaned)
    cleaned = re.sub(r"\s", "", cleaned)
    if not cleaned:
        return None
    # un nombre français : '134,50' → décimal ; '599.000' (séparateur milliers) rare ici
    # Heuristique : si une virgule, c'est le décimal ; les points sont des milliers.
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        # pas de virgule : un point isolé suivi de >2 chiffres = milliers
        if cleaned.count(".") >= 1:
            parts = cleaned.split(".")
            if all(len(p) == 3 for p in parts[1:]):
                cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned)
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
    print(f"\nTotal Abithéa: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    # contrôle de fuite hors-dept cible
    leaks = [b for b in biens if b["code_postal"] and b["code_postal"][:2] not in criteres.departements]
    print(f"FUITES hors-dept : {len(leaks)}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€ — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

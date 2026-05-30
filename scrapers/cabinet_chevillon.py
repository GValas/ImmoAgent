"""scrapers/cabinet_chevillon.py — Cabinet Chevillon (vieilles pierres Loire)

Spécialiste des demeures anciennes de la région Saumur / Anjou (49) et Touraine (37) :
châteaux, manoirs, moulins, prieurés, longères, maisons de maître en tuffeau.

Méthode : scrape_simple (httpx) — SSR WordPress immo (thème LBI / staticlbi.com).
URL pattern : /vente/{NN-dept-slug}/{page}   (ex: /vente/49-maine-et-loire/1)
              → filtre département CÔTÉ SERVEUR (vérifié : aucune fuite hors-dept).
              /biens/ et /biens/N/ redirigent vers l'accueil (pas le vrai listing).

Cartes : article.property-v2
  - URL détail : .button.js-obfuscation[data-url]  (ou a.property-v2__link[href])
       → /vente/{NN-dept}/{ville}/{type}/{tN}/{id-slug}/
  - Titre : .title__content
  - Loc   : .title__subtitle  →  "Ville (CODEPOSTAL)"
  - Texte : .property-v2__text (description, contient souvent "NN m² habitables")
  - Prix  : .property-v2__price  →  "320 000 €"
  - Photo : img.property-v2__img[data-src]  (staticlbi.com, // → https)

Type de bien : déduit du segment d'URL ({type}), p.ex. 1-maison, 22-propriete,
               28-chateau, 32-maison-de-maitre, 39-maison-de-village.
Pièces       : segment tN de l'URL (t8 → 8) si présent.
Surface hab. : pas de champ structuré → extrait de la description ("NN m² habitables").

Couverture : agence locale. Stock concentré sur 49 (Saumur/Anjou, ~20 biens) et 37
             (Touraine, quelques biens). Les autres départements cibles renvoient 0
             (normal : hors zone de l'agence) — aucune fuite.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.cabinetchevillon.fr"
MAX_PAGES = 8
PHOTOS_PER_CARD = 1

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

# Types (segment d'URL) à conserver : maisons / propriétés de caractère
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|prieure|prieuré|demeure|domaine|mas|gite|gîte|"
    r"corps-de-ferme|maison-de-village|maison-de-maitre|maison-de-maître",
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
                print(f"[CabinetChevillon] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[CabinetChevillon] Erreur dept {dept}: {e}")
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

        cards = BeautifulSoup(r.text, "html.parser").select("article.property-v2")
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

            # Sécurité : filtre serveur déjà OK, on revérifie le préfixe CP / dept-slug
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
    # URL détail : data-url (obfuscation) ou a.property-v2__link
    href = ""
    btn = card.select_one("[data-url]")
    if btn and btn.get("data-url"):
        href = btn["data-url"].strip()
    if not href:
        link = card.select_one("a.property-v2__link")
        href = link.get("href", "").strip() if link else ""
    if not href or "/vente/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    parts = [p for p in href.split("/") if p]
    # /vente/{NN-dept}/{ville}/{type}/{tN}/{id-slug}/
    # parts: ['vente', '49-maine-et-loire', '154-souzay-...', '39-maison-de-village', 't', '537-...']
    if len(parts) < 6 or parts[0] != "vente":
        return None

    type_seg = re.sub(r"^\d+-", "", parts[3])
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg.replace("-", " ").strip() or "maison"

    # pièces : segment tN
    pieces = None
    m_t = re.match(r"^t(\d+)$", parts[4])
    if m_t:
        pieces = int(m_t.group(1))

    # id_annonce : préfixe numérique du dernier segment
    id_annonce = ""
    m_id = re.match(r"^(\d+)-", parts[-1])
    if m_id:
        id_annonce = m_id.group(1)
    id_annonce = id_annonce or url

    # Localisation : "Ville (CODEPOSTAL)"
    sub_el = card.select_one(".title__subtitle")
    loc = sub_el.get_text(" ", strip=True) if sub_el else ""
    ville, code_postal = _parse_loc(loc)

    # Titre
    title_el = card.select_one(".title__content")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Description
    text_el = card.select_one(".property-v2__text")
    description = text_el.get_text(" ", strip=True) if text_el else ""

    # Prix
    price_el = card.select_one(".property-v2__price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Surface habitable : depuis la description ("NN m² habitables") puis le titre
    surface = _parse_surface_hab(description) or _parse_surface_hab(titre)

    # Photo de couverture (staticlbi.com)
    photos = []
    img = card.select_one("img.property-v2__img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http"):
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "cabinet_chevillon",
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
        "dpe": None,
        "photos": photos,
        "agence": "Cabinet Chevillon",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Souzay-Champigny (49400)' → ('Souzay-Champigny', '49400')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text.replace("\xa0", " "))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface_hab(text: str) -> float | None:
    """Cherche 'NN m² habitables', 'environ NN m2 habitables', 'NN m² hab' dans le texte."""
    if not text:
        return None
    m = re.search(
        r"(\d[\d\s\xa0]*)\s*m(?:²|2)\s*(?:hab|habitable)",
        text,
        re.IGNORECASE,
    )
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 3000:
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
    print(f"\nTotal Cabinet Chevillon: {len(biens)} annonces")
    by_dept: dict[str, int] = {}
    for b in biens:
        by_dept[b["code_postal"][:2]] = by_dept.get(b["code_postal"][:2], 0) + 1
    print(f"Par département : {dict(sorted(by_dept.items()))}")
    leaks = [b for b in biens if b["code_postal"][:2] not in
             {str(d).zfill(2) for d in criteres.departements}]
    print(f"FUITES hors-dept : {len(leaks)}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

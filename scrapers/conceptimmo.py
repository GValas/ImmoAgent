"""scrapers/conceptimmo.py — Concept Immo (agence indépendante, Cosne-Cours-sur-Loire)

Méthode : scrape_simple (httpx) — SSR HTML (CMS Modelo/Périclès, Apache, pas de Cloudflare)
URL pattern : /vente/{NN-dept-slug}/{page}   (ex: /vente/58-nievre/1, /vente/18-cher/1)
              → filtre département CÔTÉ SERVEUR. Le code dept est aussi embarqué dans
              chaque URL de détail (/vente/18-cher/...) et le CP est dans la carte
              → post-filtre code_postal[:2] strict (vérifié : aucune fuite hors-dept).

Cartes : div.item  (classe complète "property-listing-v3__item item")
  - URL   : a[href] (1er lien)  → /vente/{NN-dept}/{ville}/{N-type}/{tN}/{id-slug}/
  - Loc   : .title-subtitle__subtitle  →  "Ville (CODEPOSTAL)" (sur 2 lignes via <br>)
  - Titre : .title-subtitle__content
  - Extra : .item__info-extra  →  "129 m²" puis prix (.__price-value → "215 000 €")
  - Texte : .item__text-block (description)
  - Réf   : .item__info-id  →  "Réf : 2302"
  - Photos: img.item__img[data-src]  (URL protocole-relative //...staticlbi.com/...)

Type de bien : déduit du segment d'URL (1-maison, 22-propriete, 2-appartement,
               21-immeuble...). On ne garde que maisons / propriétés.

Couverture : agence mono/multi-départements autour de Cosne-Cours-sur-Loire.
             Stock réel observé : 58 (Nièvre) ++, 18 (Cher) ++, 45 (Loiret) +.
             72 / 28 / 89 / 41 / 37 / 49 / 36 / 53 : page valide mais 0 annonce.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.conceptimmo.net"
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

# Code département → slug URL conceptimmo.net/vente/{NN-slug}/{page}
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

# Types de bien (segment d'URL) à conserver : maisons / propriétés...
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|maison-de-village|"
    r"pavillon",
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
                print(f"[ConceptImmo] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[ConceptImmo] Erreur dept {dept}: {e}")
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

        cards = BeautifulSoup(r.text, "html.parser").select("div.item")
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

            # Filtre département strict (le filtre serveur est déjà bon, on re-vérifie)
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
    link = card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL : /vente/{NN-dept}/{ville}/{N-type}/{tN}/{id-slug}/
    parts = [p for p in href.split("/") if p]
    type_seg = parts[3] if len(parts) > 3 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean) and not _KEEP_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        # type inconnu/ambigu → on exclut par prudence
        return None
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # Référence (id_annonce)
    ref_el = card.select_one(".item__info-id")
    ref_text = ref_el.get_text(" ", strip=True) if ref_el else ""
    m_ref = re.search(r"R[ée]f\s*:?\s*([\w-]+)", ref_text, re.IGNORECASE)
    ref = m_ref.group(1) if m_ref else ""
    # id numérique du slug final en secours
    id_num = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_num = m.group(1)
    id_annonce = ref or id_num or url

    # Localisation : "Ville (CODEPOSTAL)" (texte sur 2 lignes via <br>)
    sub_el = card.select_one(".title-subtitle__subtitle")
    loc = sub_el.get_text(" ", strip=True) if sub_el else ""
    ville, code_postal = _parse_loc(loc)

    # Titre
    title_el = card.select_one(".title-subtitle__content")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Description
    text_el = card.select_one(".item__text-block")
    description = text_el.get_text(" ", strip=True) if text_el else ""

    # Bloc extra : surface ("129 m²") + prix (.__price-value)
    extra_text = ""
    extra_root = card.select_one(".group-element") or card
    for ex in extra_root.select(".item__info-extra"):
        extra_text += " " + ex.get_text(" ", strip=True)

    price_el = card.select_one(".__price-value")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    surface = _parse_surface_hab(extra_text)
    if surface is None:
        surface = _parse_surface_hab(titre) or _parse_surface_hab(description)

    # Pièces : segment tN de l'URL
    pieces = None
    if len(parts) > 4:
        m = re.match(r"^t(\d+)$", parts[4])
        if m:
            pieces = int(m.group(1))

    # Photos
    photos = []
    for img in card.select("img.item__img, img.js-lazy"):
        src = img.get("data-src") or img.get("data-lazy") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "conceptimmo",
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
        "agence": "Concept Immo",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Saint-Andelain (58150)' → ('Saint-Andelain', '58150')"""
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


def _parse_surface_hab(text: str) -> float | None:
    """Cherche 'NNN m²' dans le texte (bloc extra : '129 m²')."""
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0]*)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 2000:
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
    print(f"\nTotal Concept Immo: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

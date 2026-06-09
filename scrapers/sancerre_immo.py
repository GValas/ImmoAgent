"""scrapers/sancerre_immo.py — Sancerre Immobilier (agence indépendante, Cher 18)

Méthode : scrape_simple (httpx) — SSR HTML (CMS Modelo / staticlbi), pas de JS.
Site    : https://www.sancerre-immo.com
URL     : /vente/{page}   (ex : /vente/1, /vente/2 …)
          /fr/listing/vente redirige vers /vente/1.
          ⚠ Le slug de DÉTAIL (/vente/{ID-commune}/{type}/{id-slug}) utilise un
          identifiant interne de commune (68, 29, 48…), PAS un code département.
          → Pas de filtre serveur par département : on scrape tout l'inventaire
            (petit, ~37 biens, 4 pages) et on POST-FILTRE sur code_postal[:2].

Cartes : div.card_bien.card_bien_v2
  - URL/type/pièces : a.card_bien__link[href]  (texte : "Maison … N pièce(s)")
  - chambres/surface: li.card_bien__title_part_3  ("N chambre(s)", "NNN  m²")
  - Localisation    : .card_bien__localisation  →  "Ville (CODEPOSTAL)"
  - Prix            : .card_bien__prix  →  "265 000 €"
  - Photos          : .swiper-img[src] / source[srcset] (//sancerre-immo.staticlbi.com/…)
  - Réf (id)        : id numérique du dernier segment d'URL (ex : 371-…)

Couverture : agence du Sancerrois → ~32 biens dept 18 (Cher), ~4 dept 58 (Nièvre),
             occasionnellement 45 (Loiret). Sur la zone de test 72/28/45/89,
             seul le 45 a parfois du stock (post-filtre strict, 0 fuite).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.sancerre-immo.com"
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

# Types de bien (segment d'URL) à conserver : maisons / propriétés / fermes…
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|fermette|corps-de-ferme|"
    r"maison-de-village|grange|presbytere|presbytère",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|viager",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/vente/{page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[SancerreImmo] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                "div.card_bien.card_bien_v2"
            )
            if not cards:
                break

            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                cp = bien["code_postal"]
                # POST-FILTRE DÉPARTEMENT STRICT (0 fuite hors-zone)
                if not cp or cp[:2] not in departements:
                    continue
                bien["departement"] = cp[:2]

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
                results.append(bien)

            await asyncio.sleep(0.5)

    print(f"[SancerreImmo] {len(results)} annonces dans la zone {departements}")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a.card_bien__link")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien : 3e segment d'URL /vente/{id-commune}/{type}/{id-slug}
    parts = [p for p in href.split("/") if p]
    type_seg = parts[2] if len(parts) > 2 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean) and not _KEEP_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        return None
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # id_annonce : id numérique du dernier segment (371-sancerrois-…)
    id_num = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_num = m.group(1)
    id_annonce = id_num or url

    # Localisation : "Ville (CODEPOSTAL)"
    loc_el = card.select_one(".card_bien__localisation")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)

    # Texte du lien : "Maison … N pièce(s)"
    link_text = link.get_text(" ", strip=True)
    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", link_text)

    # chambres + surface : li.card_bien__title_part_3
    chambres = None
    surface = None
    for li in card.select(".card_bien__title_part_3"):
        t = li.get_text(" ", strip=True)
        if chambres is None:
            mc = re.search(r"(\d+)\s*chambre", t, re.IGNORECASE)
            if mc:
                chambres = int(mc.group(1))
        if surface is None:
            ms = re.search(r"([\d\s\xa0]+)\s*m²", t)
            if ms:
                val = re.sub(r"[\s\xa0]", "", ms.group(1))
                try:
                    f = float(val)
                    if 8 <= f <= 3000:
                        surface = f
                except ValueError:
                    pass

    # Prix
    price_el = card.select_one(".card_bien__prix")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Titre
    bandeau = card.select_one(".card_bien__bandeau")
    badge = bandeau.get_text(" ", strip=True) if bandeau else ""
    titre = f"{type_bien.title()} {pieces or ''} pièces {ville}".strip()
    if badge:
        titre = f"{titre} ({badge})"

    # Photos
    photos: list[str] = []
    for img in card.select(".swiper-img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    if not photos:
        for src_el in card.select("source[srcset]"):
            src = src_el.get("srcset", "").split()[0] if src_el.get("srcset") else ""
            if src:
                if src.startswith("//"):
                    src = "https:" + src
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "sancerre_immo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2] if code_postal else None,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Sancerre Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Gardefort (18300)' → ('Gardefort', '18300')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


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
    print(f"\nTotal Sancerre Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p/{b.get('chambres') or '?'}ch"
            f" — {b['type_bien']} — {b['ville']}"
        )

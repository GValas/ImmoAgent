"""scrapers/agencecotedopale.py — Agence de la Côte d'Opale (agence locale Boulogne-sur-Mer)

Méthode : scrape_simple (httpx) — SSR HTML (même CMS LBI que le_tuc.py).
URL pattern : /vente/{page}   (ex: /vente/1, /vente/2 …) — PAS de filtre département
              côté serveur (agence mono-zone Pas-de-Calais 62). On scrape la liste
              complète puis POST-FILTRE strict sur code_postal[:2] == dept.

Zone réelle : Côte d'Opale, Pas-de-Calais (62) — Boulogne-sur-Mer, Le Portel, Outreau,
              Wimille, La Capelle-lès-Boulogne… + qq biens 80 (Somme) → d'où le post-filtre.

Cartes : article.property  (identique au CMS LBI de le_tuc)
  - URL   : a.property__link[href]  → /vente/{idx-ville}/{type}/{tN}/{ref-slug}/
  - Titre : .title__content
  - Loc   : .title__subtitle  →  "Ville (CODEPOSTAL)"
  - Texte : .property__text (description, contient souvent la surface + le prix)
  - Réf   : .property__reference-number  (ex: T00207)
  - Prix  : .property__price  →  "433 000 €"
  - Opts  : .option  →  <title>Nombre de pièces</title> + .option__number
  - Photos: .property__img[data-src]

Type de bien : déduit du segment d'URL (.../{type}/...). On ne garde que maisons/propriétés.

Couverture (cible Val-de-Loire/Ouest) : NULLE — agence exclusivement Côte d'Opale (62).
             Aucun chevauchement avec les départements cibles → 0 bien attendu en zone.
             Scraper fonctionnel conservé (actif: false) ; réactiver si la zone s'étend au 62.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.agencecotedopale.fr"
MAX_PAGES = 12
PHOTOS_PER_CARD = 10


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
                print(f"[CoteOpale] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("article.property")
            if not cards:
                break

            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # Post-filtre département STRICT (aucun filtre serveur ; site 62/80)
                cp = bien.get("code_postal") or ""
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

    # Récap par département
    for dept in departements:
        n = sum(1 for b in results if b["departement"] == dept)
        if n:
            print(f"[CoteOpale] Dept {dept}: {n} annonces")
    if not results:
        print("[CoteOpale] 0 annonce en zone (agence Côte d'Opale / 62 uniquement)")

    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a.property__link")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL : /vente/{idx-ville}/{type}/{tN}/{ref-slug}/
    parts = [p for p in href.split("/") if p]
    # parts: ['vente', '{idx-ville}', '{type}', '{tN}', '{ref-slug}']
    type_seg = parts[2] if len(parts) > 2 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean) and not _KEEP_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        return None
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # Référence (id_annonce)
    ref_el = card.select_one(".property__reference-number")
    ref = ref_el.get_text(strip=True) if ref_el else ""
    id_num = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_num = m.group(1)
    id_annonce = ref or id_num or url

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
    text_el = card.select_one(".property__text")
    description = text_el.get_text(" ", strip=True) if text_el else ""

    # Prix
    price_el = card.select_one(".property__price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Options : pièces / chambres (blocs .option avec <title> + .option__number)
    pieces = chambres = None
    for opt in card.select(".option"):
        t_el = opt.find("title")
        n_el = opt.select_one(".option__number")
        if not t_el or not n_el:
            continue
        label = t_el.get_text(strip=True).lower()
        num = _safe_int(n_el.get_text(strip=True))
        if num is None:
            continue
        if "pièce" in label or "piece" in label:
            pieces = num
        elif "chambre" in label:
            chambres = num

    # Pièces en secours : segment tN de l'URL
    if pieces is None:
        for seg in parts:
            m = re.match(r"^t(\d+)$", seg)
            if m:
                pieces = int(m.group(1))
                break

    # Surface habitable : pas dans les options → depuis description/titre
    surface = _parse_surface_hab(description) or _parse_surface_hab(titre)

    # Photos
    photos = []
    for img in card.select(".property__img"):
        src = img.get("data-src") or img.get("data-lazy") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "agencecotedopale",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
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
        "agence": "Agence de la Côte d'Opale",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'La Capelle-lès-Boulogne (62360)' → ('La Capelle-lès-Boulogne', '62360')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _safe_int(text: str) -> int | None:
    m = re.search(r"\d+", text or "")
    return int(m.group(0)) if m else None


def _parse_surface_hab(text: str) -> float | None:
    """Cherche 'surface habitable de NNN m²' ou 'NNN m² hab' dans le texte libre."""
    if not text:
        return None
    m = re.search(
        r"surface\s+habitable[^0-9]*([\d\s\xa0]+(?:[.,]\d+)?)\s*m",
        text,
        re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r"([\d\s\xa0]+(?:[.,]\d+)?)\s*m²?\s*(?:hab|habitable)",
            text,
            re.IGNORECASE,
        )
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
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
    print(f"\nTotal Agence Côte d'Opale: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

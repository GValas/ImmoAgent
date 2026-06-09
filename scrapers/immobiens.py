"""scrapers/immobiens.py — Bien(s) / Immobiens (Laval & Mayenne, 53)

Méthode : scrape_simple (httpx) — SSR HTML (CMS "W3D / Periimmo"), microdata schema.org.
URL pattern : /immobilier-Mayenne.htm
              → page dédiée DÉPARTEMENT Mayenne (filtre côté serveur : tout le
                stock est en 53). Pas de pagination (petite agence, une seule page).
                POST-FILTRE strict CP[:2] en plus, par sécurité (0 fuite).

Cartes : div.res_div_container
  - URL    : a[href*="/immobilier/"]
             → /immobilier/{type}-{N}-pieces-{ville}-{CP}-fr_{REF}.htm
               le slug d'URL contient TYPE + PIÈCES + VILLE + CODE POSTAL (fiable).
  - Texte  : prix "214 120 €", "Maison individuelle Nuillé-sur-Vicoin",
             surface "110 m²", description courte.

Type de bien : déduit du slug d'URL (maison / longère / manoir / appartement /
               terrain...). On ne garde que maisons / propriétés.

Couverture : agence Laval (53) — stock réel modeste, bien renseigné.
             dernier_test 2026-06-09.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.immobiens.fr"
# Pages départementales SSR (une par dept couvert par l'agence — surtout 53).
DEPT_PAGES: dict[str, str] = {
    "53": "/immobilier-Mayenne.htm",
}
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|pavillon|grange|fermette|bastide",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"boutique|hangar|studio|investissement",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            path = DEPT_PAGES.get(dept)
            if not path:
                continue
            try:
                r = await client.get(BASE_URL + path)
            except Exception as e:
                print(f"[Immobiens] Erreur dept {dept}: {e}")
                continue
            if r.status_code != 200:
                continue

            cards = BeautifulSoup(r.text, "html.parser").select(
                "div.res_div_container"
            )
            n_dept = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # POST-FILTRE dept STRICT (0 fuite)
                if not bien["code_postal"] or bien["code_postal"][:2] not in departements:
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
                results.append(bien)
                n_dept += 1
            print(f"[Immobiens] Dept {dept}: {n_dept} annonces")
            await asyncio.sleep(0.5)

    print(f"[Immobiens] Total: {len(results)} annonces")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one('a[href*="/immobilier/"]')
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Slug : /immobilier/maison-manoir-8-pieces-quelaines-saint-gault-53360-fr_VM1276.htm
    slug = href.rsplit("/", 1)[-1]
    type_bien = _detect_type(slug)
    if type_bien is None:
        return None

    code_postal = _cp_from_slug(slug)
    ville = _ville_from_slug(slug, code_postal)
    pieces = _parse_int(r"(\d+)-pieces?", slug) or _parse_int(r"(\d+)\s*pi[eè]ce",
                                                              card.get_text(" "))

    full_text = card.get_text(" ", strip=True)

    # Titre : ligne « <Type> <Ville> » dans le texte de la carte
    titre = _card_title(card) or f"{type_bien.title()} {ville}".strip()

    prix = _parse_price(full_text)
    surface = _parse_surface(full_text)
    chambres = _parse_int(r"(\d+)\s*chambre", full_text)

    ref = _ref_from_slug(slug)
    id_annonce = ref or url

    photos = []
    img = card.select_one("img")
    if img:
        src = img.get("src") or img.get("data-src") or img.get("data-original") or ""
        if src and not src.startswith("data:"):
            photos.append(src if src.startswith("http") else BASE_URL + src)

    return {
        "source": "immobiens",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": full_text[:1200],
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Bien(s) Immobiens",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _detect_type(slug: str) -> str | None:
    s = slug.replace("-", " ")
    if _EXCLUDE_TYPE.search(s) and not _KEEP_TYPE.search(s):
        return None
    m = _KEEP_TYPE.search(s)
    if m:
        return m.group(0).lower()
    return None


def _cp_from_slug(slug: str) -> str:
    m = re.search(r"-(\d{5})-fr_", slug)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{5})\b", slug)
    return m.group(1) if m else ""


def _ville_from_slug(slug: str, cp: str) -> str:
    """Extrait la ville entre 'N-pieces-' et '-CP-fr_'."""
    m = re.search(r"\d+-pieces?-(.+?)-\d{5}-fr_", slug)
    if m:
        return m.group(1).replace("-", " ").strip().title()
    # repli : segment avant le CP
    if cp:
        m = re.search(r"([a-zà-ÿ\-]+)-" + re.escape(cp), slug, re.IGNORECASE)
        if m:
            return m.group(1).replace("-", " ").strip().title()
    return ""


def _ref_from_slug(slug: str) -> str:
    m = re.search(r"fr_([A-Za-z0-9]+)\.htm", slug)
    return m.group(1) if m else ""


def _card_title(card) -> str:
    # Le bloc texte central contient une ligne courte type « Maison individuelle Ville ».
    for el in card.select("a, h2, h3, .res_titre, strong, b"):
        t = el.get_text(" ", strip=True)
        if t and _KEEP_TYPE.search(t) and len(t) < 90 and "€" not in t:
            return t
    return ""


def _parse_price(text: str) -> float | None:
    m = re.search(r"([\d\s\xa0]{4,})\s*€", text)
    if not m:
        return None
    cleaned = re.sub(r"[^\d]", "", m.group(1))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_surface(text: str) -> float | None:
    # surface habitable explicite d'abord
    m = re.search(r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m²?\s*hab", text, re.IGNORECASE)
    if not m:
        # 1er 'NNN m²' non précédé de 'terrain'
        for cand in re.finditer(r"(\d{2,4}(?:[.,]\d+)?)\s*m²", text):
            prefix = text[max(0, cand.start() - 12):cand.start()].lower()
            if "terrain" in prefix:
                continue
            m = cand
            break
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
    print(f"\nTotal Immobiens: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

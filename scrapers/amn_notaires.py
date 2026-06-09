"""scrapers/amn_notaires.py — Anjou Maine Notaires (amn.notaires.fr)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress / thème PowerPack).
Site    : office notarial interdépartemental Anjou/Maine. Ventes notaires.
          Distinct des portails notaires déjà couverts (notaires_valdeloire,
          mdi_anjoumaine, immobilier_notaires, immonot).

URL pattern : /annonces-immmobilieres/            (page 1, noter le triple "m")
              /annonces-immmobilieres/page/{N}/    (pagination, 24 cartes/page)
          → PAS de filtre département côté serveur. La liste est nationale au sens
            du périmètre de l'office : majoritairement Sarthe (72) + quelques biens
            hors-zone (85). On POST-FILTRE strictement sur code_postal[:2].

Cartes : div.pp-content-grid-post
  - URL    : a[href*="/bien_immobilier/"]
  - Loc    : .libelle                       → "Ville (CODEPOSTAL)"
  - Titre  : .pp-content-grid-post-title     → "maison - 6 pièce(s) - 178 m 2"
             (type, pièces, surface habitable au même endroit)
  - Prix   : .montant (total FAI) / .prix_de_vente (net vendeur)
  - Texte  : .pp-content-grid-post-excerpt
  - Réf    : texte "Référence : 72068-2062"  → id_annonce stable
  - Photo  : img.wp-post-image[src]

Type de bien : champ "terrain"/"autre"/"maison"/"appartement"... dans le titre.
               On ne garde que maisons / propriétés (exclut terrain, appartement,
               local, garage...).

Volume observé (2026-06) : 168 biens au total → 161 en 72, 7 en 85 (hors-zone,
               filtrés). Aucune fuite après post-filtre code_postal[:2].

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://amn.notaires.fr"
LIST_URL = f"{BASE_URL}/annonces-immmobilieres"
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

# Types de bien à conserver (maisons / propriétés) — depuis le champ titre.
_KEEP_TYPE = re.compile(
    r"maison|propriet|villa|ferme|longere|longère|manoir|chateau|château|"
    r"moulin|demeure|domaine|mas|gite|gîte|corps de ferme|maison de village|"
    r"pavillon|fermette",
    re.IGNORECASE,
)
# Types explicitement exclus.
_EXCLUDE_TYPE = re.compile(
    r"terrain|appartement|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|cave|box|viager|investissement|murs",
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
            url = LIST_URL + "/" if page == 1 else f"{LIST_URL}/page/{page}/"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[AMNotaires] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                "div.pp-content-grid-post"
            )
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # Post-filtre département STRICT (0 fuite hors-zone).
                cp = bien["code_postal"]
                if not cp or cp[:2] not in departements:
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

                bien["departement"] = cp[:2]
                seen_ids.add(aid)
                results.append(bien)
                new_on_page += 1

            await asyncio.sleep(0.5)

    # Récap par département (visibilité fuite).
    par_dept: dict[str, int] = {}
    for b in results:
        par_dept[b["departement"]] = par_dept.get(b["departement"], 0) + 1
    print(f"[AMNotaires] Total {len(results)} annonces — par dept {par_dept}")

    return results


def _parse_card(card) -> dict | None:
    link = card.select_one('a[href*="/bien_immobilier/"]')
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Localisation : ".libelle"  →  "Ville (72300)"
    loc_el = card.select_one(".libelle")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)
    if not code_postal:
        return None

    # Titre : "maison - 6 pièce(s) - 178 m 2"
    title_el = card.select_one(".pp-content-grid-post-title")
    title_txt = title_el.get_text(" ", strip=True) if title_el else ""

    type_bien = _parse_type(title_txt)
    if not type_bien:
        return None

    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", title_txt)
    surface = _parse_surface(title_txt)

    # Prix : ".montant" (total FAI) ; secours ".prix_de_vente".
    prix = _parse_price_el(card.select_one(".montant"))
    if prix is None:
        prix = _parse_price_el(card.select_one(".prix_de_vente"))

    # Référence (id_annonce stable) : "Référence : 72068-2062"
    ref = ""
    ref_node = card.find(string=re.compile(r"R[ée]f[ée]rence", re.IGNORECASE))
    if ref_node:
        m = re.search(r"R[ée]f[ée]rence\s*:?\s*([\w\-/]+)", str(ref_node))
        if m:
            ref = m.group(1).strip()
    # secours : id du post WordPress.
    if not ref:
        for cls in card.get("class", []):
            m = re.match(r"post-(\d+)$", cls)
            if m:
                ref = m.group(1)
                break
    id_annonce = ref or url

    # Description.
    exc_el = card.select_one(".pp-content-grid-post-excerpt")
    description = exc_el.get_text(" ", strip=True) if exc_el else ""

    # Photo.
    photos: list[str] = []
    for img in card.select("img"):
        src = (
            img.get("data-lazy-src")
            or img.get("data-src")
            or img.get("src")
            or ""
        )
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    # dédup en conservant l'ordre.
    photos = list(dict.fromkeys(photos))[:PHOTOS_PER_CARD]

    titre = title_txt or f"{type_bien.title()} {ville}".strip()

    return {
        "source": "amn_notaires",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Anjou Maine Notaires",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Juigné-sur-Sarthe (72300)' → ('Juigné-sur-Sarthe', '72300')"""
    cp = ""
    m = re.search(r"\((\d{5})\)", text)
    if m:
        cp = m.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_type(title_txt: str) -> str | None:
    """Extrait le type depuis 'maison - 6 pièce(s) - 178 m 2' ; None si à exclure."""
    # Le type est le 1ᵉʳ segment avant le 1ᵉʳ tiret.
    head = title_txt.split("-", 1)[0].strip().lower()
    candidate = head or title_txt.lower()
    if _EXCLUDE_TYPE.search(candidate) and not _KEEP_TYPE.search(candidate):
        return None
    if not _KEEP_TYPE.search(candidate):
        # type inconnu/ambigu ("autre", vide...) → exclu par prudence.
        return None
    return candidate.replace("(s)", "").strip() or "maison"


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_surface(title_txt: str) -> float | None:
    """'maison - 6 pièce(s) - 178 m 2' → 178.0 (gère 'm 2' / 'm²')."""
    m = re.search(r"(\d[\d\s\xa0]*)\s*m\s*[²2]", title_txt)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 5000:
                return f
        except ValueError:
            pass
    return None


def _parse_price_el(el) -> float | None:
    if el is None:
        return None
    txt = el.get_text(" ", strip=True)
    cleaned = re.sub(r"[^\d.]", "", re.sub(r"[\s\xa0€]", "", txt))
    # Retirer un éventuel point final orphelin.
    cleaned = cleaned.rstrip(".")
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
    print(f"\nTotal AMN Notaires: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

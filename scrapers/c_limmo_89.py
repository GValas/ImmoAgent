"""scrapers/c_limmo_89.py — C-l'immo (agence indépendante, Sens / Yonne)

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /immobilier/vente            (page 1)
              /immobilier/vente{N}          (pages 2..N, ex: /immobilier/vente2)
              → PAS de filtre département côté serveur : l'agence diffuse sur
                plusieurs départements (89 Yonne, 45 Loiret, 77 Seine-et-Marne,
                10 Aube, 91, 94…). On scrape tout le « vente » puis on
                POST-FILTRE STRICTEMENT sur code_postal[:2] ∈ départements cibles.

Cartes : a.annonce
  - URL   : href de a.annonce → /immobilier/annonce-{slug}-{ref}
  - Réf   : nombre final du slug d'URL (ou title="...réf. : XXXX")
  - Titre : h3
  - Loc   : .ville  →  "Ville (CODEPOSTAL)"
  - Surf  : .infos  →  "Surface de NNN m²"  (habitable pour le bâti,
            superficie de terrain pour les lots à bâtir → on exclut les terrains)
  - Prix  : .prix   →  "189 000 €"
  - Texte : <p> (premier paragraphe = description tronquée)
  - Photo : section.photo[style=background-image:url('...')]

Type de bien : déduit du titre/slug. On ne garde que maisons / propriétés /
               fermes / longères ; on exclut terrain / appartement / commerce /
               immeuble / local / garage.

Couverture : agence mono-implantation ; ~229 biens tous départements confondus,
             dont 89 (~120) et 45 (~67) dans la zone cible. Le reste (77/10/91/94)
             est écarté par le post-filtre → 0 fuite.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.c-limmo.fr"
MAX_PAGES = 30
PHOTOS_PER_CARD = 1  # la vue liste n'expose qu'une photo de fond

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien (titre/slug) à conserver
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|fermette|longere|longère|manoir|"
    r"chateau|château|moulin|demeure|domaine|mas|gite|gîte|corps de ferme|"
    r"maison de village|pavillon|plain[- ]pied|maisonnette|maisonette|bâtisse|batisse",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"terrain|appartement|appart|local|commerce|garage|box|parking|immeuble|"
    r"bureau|fonds|fond de commerce|salon de|hangar|entrep[oô]t|grange seule",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()
    seen_hrefs: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = (
                f"{BASE_URL}/immobilier/vente"
                if page == 1
                else f"{BASE_URL}/immobilier/vente{page}"
            )
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[C-l'immo] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("a.annonce")
            if not cards:
                break

            hrefs_on_page = {c.get("href", "") for c in cards}
            # Au-delà de la dernière page réelle, le site re-sert la page 1 :
            # si aucun href de la page n'est nouveau, on a bouclé → stop.
            if page > 1 and seen_hrefs and hrefs_on_page <= seen_hrefs:
                break
            seen_hrefs |= hrefs_on_page

            for card in cards:
                try:
                    bien = _parse_card(card, departements)
                except Exception:
                    continue
                if not bien:
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

            await asyncio.sleep(0.5)

    in_zone = [b for b in results if b["code_postal"][:2] in departements]
    by_dept: dict[str, int] = {}
    for b in in_zone:
        by_dept[b["code_postal"][:2]] = by_dept.get(b["code_postal"][:2], 0) + 1
    print(f"[C-l'immo] {len(in_zone)} annonces dans la zone — détail {by_dept}")
    return in_zone


def _parse_card(card, departements: set[str]) -> dict | None:
    href = card.get("href", "")
    if not href or "/immobilier/annonce-" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Localisation : "Ville (CODEPOSTAL)"
    ville_el = card.select_one(".ville")
    loc = ville_el.get_text(" ", strip=True) if ville_el else ""
    ville, code_postal = _parse_loc(loc)

    # Post-filtre département STRICT (le site n'a pas de filtre serveur fiable)
    if not code_postal or code_postal[:2] not in departements:
        return None

    # Titre
    h3 = card.select_one("h3")
    titre = h3.get_text(" ", strip=True) if h3 else ""

    # Type de bien : titre + slug d'URL
    type_text = f"{titre} {href}"
    # Un titre/slug qui COMMENCE par "terrain" est un lot à bâtir même s'il
    # évoque une « maison individuelle » à construire → on exclut.
    if re.match(r"\s*terrain\b", titre, re.IGNORECASE) or "annonce-terrain" in href:
        return None
    if _EXCLUDE_TYPE.search(type_text) and not _KEEP_TYPE.search(type_text):
        return None
    if not _KEEP_TYPE.search(type_text):
        return None  # type inconnu/ambigu → exclu par prudence
    type_bien = _guess_type(type_text)

    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Référence (id_annonce) : nombre final du slug, ou title="réf. : XXXX"
    ref = ""
    m_ref = re.search(r"r[ée]f\.?\s*:?\s*([A-Za-z0-9]+)", card.get("title", ""))
    if m_ref:
        ref = m_ref.group(1)
    if not ref:
        m_slug = re.search(r"-(\d+[a-z]?)$", href)
        if m_slug:
            ref = m_slug.group(1)
    id_annonce = ref or url

    # Prix
    prix_el = card.select_one(".prix")
    prix = _parse_price(prix_el.get_text(" ", strip=True) if prix_el else "")

    # Surface (habitable pour le bâti). On corrobore avec le titre si possible.
    infos_el = card.select_one(".infos")
    infos = infos_el.get_text(" ", strip=True) if infos_el else ""
    surface = _parse_surface(infos)

    # Description (premier <p> non-italique = extrait)
    description = ""
    for p in card.select("section.texte p"):
        if p.find("i"):
            continue
        txt = p.get_text(" ", strip=True)
        if txt:
            description = txt
            break

    # Photo de fond
    photos: list[str] = []
    photo_el = card.select_one("section.photo")
    if photo_el:
        style = photo_el.get("style", "")
        m_img = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", style)
        if m_img:
            src = m_img.group(1)
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http"):
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "c_limmo_89",
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
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "C-l'immo",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _guess_type(text: str) -> str:
    low = text.lower()
    for kw, label in [
        ("longere", "longère"),
        ("longère", "longère"),
        ("fermette", "fermette"),
        ("ferme", "ferme"),
        ("manoir", "manoir"),
        ("chateau", "château"),
        ("château", "château"),
        ("moulin", "moulin"),
        ("propriete", "propriété"),
        ("propriété", "propriété"),
        ("villa", "villa"),
        ("pavillon", "pavillon"),
        ("plain", "maison de plain-pied"),
        ("maison de village", "maison de village"),
    ]:
        if kw in low:
            return label
    return "maison"


def _parse_loc(text: str) -> tuple[str, str]:
    """'Sens (89100)' → ('Sens', '89100')"""
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


def _parse_surface(text: str) -> float | None:
    """'Surface de 140 m²' → 140.0"""
    if not text:
        return None
    m = re.search(r"([\d\s\xa0]+)\s*m", text)
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
    print(f"\nTotal C-l'immo: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

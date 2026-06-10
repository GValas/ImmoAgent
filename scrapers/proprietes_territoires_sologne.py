"""scrapers/proprietes_territoires_sologne.py — Propriétés et Territoires (Sologne)

Agence de prestige basée à Romorantin-Lanthenay (41200), spécialisée Sologne :
demeures d'exception, corps de ferme, domaines de chasse, étangs, forêts.

Méthode : scrape_simple (httpx) — SSR HTML, thème WordPress WpResidence.
          Aucun JS requis, aucun anti-bot (hébergement o2switch, HTTP 200).

Stratégie :
  - Pas de page liste paginée fiable (la pagination /page/N est factice :
    elle renvoie toujours les 10 mêmes cartes les plus récentes).
  - On lit donc le **sitemap des biens** `estate_property-sitemap.xml`, qui
    contient l'inventaire COMPLET (~34 URLs détail), puis on ouvre chaque fiche.

Fiche détail (div.listing_detail / classes WpResidence) :
  - Titre  : <h1> / og:title
  - Réf    : .property_internal_id            → "Réf. web: 21056"
  - Prix   : .property_default_price          → "Prix : 65 000€ F.A.I"
  - Surface: .listing_detail contenant "Surface habitable: NN m 2"
  - Terrain: .property_default_lot_size       → "Superficie terrain en M²: 519 m 2"
  - Chambres : .listing_detail "Chambres : N"
  - Ville  : .wpresidence-detail-ville        → "Ville: Romorantin-Lanthenay"
  - CP     : .wpresidence-detail-code-postal  → "Code postal : 41200"  ← filtre dept
  - DPE    : .listing_detail "Classe énergétique (DPE): F"
  - Desc   : og:description / bloc description
  - Photos : img wp-content/uploads (galerie)

Filtre département : AUCUN filtre serveur (agence mono-zone Sologne). Le
département est déduit du **code postal de la fiche** (`code_postal[:2]`),
avec post-filtre STRICT sur les départements cibles → 0 fuite. La Sologne
chevauche 41 (Loir-et-Cher), 45 (Loiret) et 18 (Cher), tous dans la zone.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://proprietes-territoires-sologne.fr"
SITEMAP_URL = f"{BASE_URL}/estate_property-sitemap.xml"
MAX_PROPERTIES = 80          # garde-fou (inventaire réel ~34)
PHOTOS_PER_CARD = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types exclus (on garde maisons / propriétés / fermes / domaines / étangs / forêts)
_EXCLUDE_TYPE = re.compile(
    r"\bappartement\b|\bgarage\b|\bparking\b|\bbureau\b|\blocal commercial\b",
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
        urls = await _list_property_urls(client)
        print(f"[PropTerritoiresSologne] {len(urls)} biens dans le sitemap")

        for url in urls[:MAX_PROPERTIES]:
            try:
                bien = await _scrape_detail(client, url)
            except Exception as e:
                print(f"[PropTerritoiresSologne] Erreur {url}: {e}")
                bien = None
            await asyncio.sleep(0.5)

            if not bien:
                continue

            cp = bien.get("code_postal") or ""
            dept = cp[:2] if len(cp) >= 2 else ""
            # Post-filtre dept STRICT : sans CP exploitable → on écarte (pas de fuite)
            if dept not in departements:
                continue
            bien["departement"] = dept

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

    print(f"[PropTerritoiresSologne] {len(results)} biens retenus dans la zone")
    return results


async def _list_property_urls(client: httpx.AsyncClient) -> list[str]:
    """Inventaire complet via le sitemap WpResidence des biens."""
    r = await client.get(SITEMAP_URL)
    if r.status_code != 200:
        return []
    locs = re.findall(r"<loc>(.*?)</loc>", r.text)
    urls = []
    for loc in locs:
        loc = loc.strip()
        # /proprietes-en-sologne/{slug}/ ; on exclut la racine d'archive
        if "/proprietes-en-sologne/" not in loc:
            continue
        slug = loc.rstrip("/").rsplit("/", 1)[-1]
        if slug == "proprietes-en-sologne":   # page archive, pas un bien
            continue
        urls.append(loc)
    return urls


async def _scrape_detail(client: httpx.AsyncClient, url: str) -> dict | None:
    r = await client.get(url)
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    # ── Titre
    h1 = soup.select_one("h1")
    og_t = soup.select_one('meta[property="og:title"]')
    titre = ""
    if h1:
        titre = h1.get_text(" ", strip=True)
    if not titre and og_t:
        titre = og_t.get("content", "")
    titre = titre.strip()

    # ── Référence (id_annonce)
    ref = ""
    ref_el = soup.select_one(".property_internal_id")
    if ref_el:
        m = re.search(r"(\d+)", ref_el.get_text(" ", strip=True))
        if m:
            ref = m.group(1)
    id_annonce = ref or url.rstrip("/").rsplit("/", 1)[-1]

    # ── Localisation
    ville = _label_value(soup, ".wpresidence-detail-ville")
    cp_raw = _label_value(soup, ".wpresidence-detail-code-postal")
    m_cp = re.search(r"\b(\d{5})\b", cp_raw)
    code_postal = m_cp.group(1) if m_cp else ""

    # Si la classe CP est absente, tentative via l'adresse / régions n'aide pas
    # (pas de CP fiable) → on laisse vide, le post-filtre écartera le bien.

    # ── Prix
    prix = _parse_price(_text(soup, ".property_default_price"))

    # ── Surfaces / pièces
    details_text = " ".join(
        d.get_text(" ", strip=True) for d in soup.select(".listing_detail")
    )
    surface = _parse_m2(r"Surface habitable[^0-9]*([\d\s.,]+)", details_text)
    surface_terrain = _parse_m2(
        r"Superficie terrain[^0-9]*([\d\s.,]+)", details_text
    )
    chambres = _parse_int(r"Chambres?\s*:?\s*(\d+)", details_text)

    # ── DPE (classe énergétique, on ignore le GES)
    dpe = None
    m_dpe = re.search(
        r"Classe[ ]énergétique[ ]\(DPE\)\s*:?\s*([A-G])", details_text
    )
    if m_dpe:
        dpe = m_dpe.group(1).upper()

    # ── Type de bien depuis le titre/slug
    type_bien = _guess_type(titre)
    if _EXCLUDE_TYPE.search(titre):
        return None

    # ── Description
    description = ""
    og_d = soup.select_one('meta[property="og:description"]')
    if og_d:
        description = og_d.get("content", "").strip()
    if len(description) < 40:
        body = soup.select_one(".wpestate_estate_property_details_panel, .panel-body")
        if body:
            description = body.get_text(" ", strip=True)

    # ── Photos (galerie wp uploads, hors logos/icônes)
    photos: list[str] = []
    for img in soup.select("img"):
        src = img.get("data-src") or img.get("data-original") or img.get("src") or ""
        if not src or src.startswith("data:"):
            continue
        if "/wp-content/uploads/" not in src:
            continue
        if src not in photos:
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    return {
        "source": "proprietes_territoires_sologne",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Propriétés et Territoires",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _text(soup, sel: str) -> str:
    e = soup.select_one(sel)
    return e.get_text(" ", strip=True) if e else ""


def _label_value(soup, sel: str) -> str:
    """Récupère la valeur d'un champ 'Label: valeur' (retire le libellé)."""
    raw = _text(soup, sel)
    if ":" in raw:
        return raw.split(":", 1)[1].strip()
    return raw.strip()


def _parse_price(text: str) -> float | None:
    # "Prix : 470 400€ F.A.I" → 470400
    m = re.search(r"([\d][\d\s .]*)\s*€", text)
    if not m:
        return None
    cleaned = re.sub(r"[\s .]", "", m.group(1))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_m2(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    # "375,800" (séparateur de milliers FR) ou "519" ou "70"
    raw = m.group(1).strip()
    raw = re.sub(r"[\s ]", "", raw)
    raw = raw.replace(",", "")   # 375,800 m² = 375800 m² (milliers)
    raw = raw.rstrip(".")
    try:
        val = float(raw)
        if 0 < val <= 50_000_000:
            return val
    except ValueError:
        pass
    return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _guess_type(titre: str) -> str:
    t = (titre or "").lower()
    for kw, label in [
        ("château", "château"), ("chateau", "château"),
        ("manoir", "manoir"), ("moulin", "moulin"),
        ("domaine", "domaine"), ("corps de ferme", "corps de ferme"),
        ("ferme", "ferme"), ("fermette", "fermette"),
        ("étang", "étang"), ("etang", "étang"),
        ("forêt", "forêt"), ("foret", "forêt"),
        ("territoire", "territoire de chasse"),
        ("chasse", "territoire de chasse"),
        ("propriété", "propriété"), ("propriete", "propriété"),
        ("pavillon", "maison"), ("maison", "maison"),
    ]:
        if kw in t:
            return label
    return "propriété"


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
    print(f"\nTotal Propriétés et Territoires (Sologne): {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

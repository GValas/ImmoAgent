"""scrapers/domaines_forets.py — Domaines & Forêts (immobilier forestier / chasse / pêche)

Méthode : scrape_simple (httpx) — SSR (WordPress + WooCommerce, hébergement o2switch).
Segment : forêts, massifs, étangs, territoires de chasse/pêche, propriétés d'agrément.

Stratégie :
  1. On récupère le catalogue complet via le sitemap WooCommerce produit :
     /product-sitemap1.xml  →  ~185 URLs /bien-immobilier/{slug}/
     (la Store API /wp-json/wc/store/... renvoie 401 — sitemap = source fiable).
  2. Le département est encodé DANS le slug (= le titre) par son NOM
     (ex: '...dans-le-cher-18', '...dans-lindre-36', '...dans-le-loiret',
      '...dans-lyonne-89', '...loir-et-cher-41'). Il n'existe PAS de paramètre
     ?dep= fiable, et le code à 2 chiffres présent dans certains slugs est
     AMBIGU (souvent un nombre d'hectares : 'foret-45-ha-dans-laisne' est dans
     l'Aisne, pas le Loiret). → On résout le département par NOM de département
     via DEPT_NAME_PATTERNS, motifs ordonnés du plus spécifique au plus large
     (indre-et-loire AVANT indre, loir-et-cher AVANT cher, eure-et-loir AVANT
      eure). On ne fetch que les biens dont le dept résolu ∈ départements cibles.
  3. Page détail (SSR) : titre h1.product_title, prix via meta
     <meta property="product:price:amount">, description via meta description,
     image via og:image (la galerie est en lazy-load, absente du HTML brut).

Post-filtre dept STRICT : on ne garde QUE les biens dont le dept résolu par nom
est dans la zone cible → 0 fuite hors-zone (le code chiffré du slug n'est jamais
utilisé seul pour décider, car ambigu avec les hectares).

Type de bien : forêt / massif forestier / étang / propriété de chasse… (segment
de niche, pas de maison classique en général). On renseigne type_bien depuis le
titre.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.domaines-forets.fr"
SITEMAP_URL = f"{BASE_URL}/product-sitemap1.xml"
MAX_DETAILS = 60  # garde-fou sur le nb de pages détail fetchées par run

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Département → motifs (regex) cherchés dans le SLUG (titre slugifié).
# ORDRE IMPORTANT : composés (indre-et-loire) AVANT composants (indre).
# On itère cette liste dans l'ordre et on prend le 1er match → priorité au plus
# spécifique. Couvre largement la France pour pouvoir EXCLURE proprement les
# biens hors-zone, mais seuls les depts demandés sont retenus.
DEPT_NAME_PATTERNS: list[tuple[str, list[str]]] = [
    # ── Composés (doivent passer avant leurs composants) ──
    ("37", [r"indre-et-loire", r"\bindre-et-loire", r"touraine", r"gatine-tourangelle"]),
    ("41", [r"loir-et-cher", r"\bsologne\b"]),  # Sologne ≈ majoritairement 41
    ("28", [r"eure-et-loir"]),
    ("77", [r"seine-et-marne"]),
    ("47", [r"lot-et-garonne"]),
    ("71", [r"saone-et-loire"]),
    # ── Zone cible (noms simples) ──
    ("45", [r"\bloiret\b"]),
    ("89", [r"lyonne\b", r"l-yonne\b", r"\byonne\b"]),
    ("72", [r"\bsarthe\b"]),
    ("36", [r"lindre\b", r"-indre\b", r"\bindre-", r"\bbrenne\b"]),  # Indre / Parc de la Brenne
    ("18", [r"le-cher\b", r"du-cher\b", r"-cher\b", r"\bcher-"]),
    # ── Autres (pour exclusion explicite) ──
    ("27", [r"leure\b", r"-eure\b", r"\beure-", r"\bvexin\b"]),  # Eure / Vexin
    ("91", [r"lessonne\b", r"-essonne\b"]),
    ("79", [r"deux-sevres", r"sevres"]),
    ("85", [r"vendee"]),
    ("31", [r"haute-garonne"]),
    ("83", [r"\bvar\b"]),
    ("66", [r"pyrenees-orientales"]),
    ("15", [r"cantal"]),
    ("02", [r"laisne\b", r"-aisne\b"]),
    ("58", [r"nievre"]),
    ("21", [r"cote-dor"]),
    ("89", [r"bourgogne"]),  # repli région (Yonne fait partie de la Bourgogne)
]

_TYPE_RE = re.compile(
    r"(massif forestier|propri[ée]t[ée] foresti[èe]re|for[êe]t|[ée]tang|"
    r"domaine de chasse|propri[ée]t[ée] de chasse|territoire|corps de ferme|"
    r"longere|long[èe]re|fermette|propri[ée]t[ée] [ée]questre|propri[ée]t[ée])",
    re.IGNORECASE,
)


def _dept_from_slug(slug: str) -> str | None:
    """Résout le département par NOM dans le slug. Retourne le code ou None."""
    s = slug.lower()
    for code, patterns in DEPT_NAME_PATTERNS:
        for pat in patterns:
            if re.search(pat, s):
                return code
    return None


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1. Catalogue complet via sitemap
        try:
            r = await client.get(SITEMAP_URL)
            r.raise_for_status()
        except Exception as e:
            print(f"[DomainesForets] Erreur sitemap: {e}")
            return results

        urls = re.findall(r"<loc>(.*?)</loc>", r.text)
        urls = [u for u in urls if "/bien-immobilier/" in u]

        # 2. Pré-filtre par dept résolu sur le NOM (jamais sur le code ambigu)
        candidats: list[tuple[str, str]] = []  # (url, dept)
        for u in urls:
            slug = u.split("/bien-immobilier/")[-1].rstrip("/")
            dept = _dept_from_slug(slug)
            if dept and dept in departements:
                candidats.append((u, dept))

        print(
            f"[DomainesForets] {len(urls)} biens au catalogue, "
            f"{len(candidats)} dans la zone cible {departements}"
        )

        # 3. Fetch des pages détail (uniquement les candidats de la zone)
        for url, dept in candidats[:MAX_DETAILS]:
            try:
                bien = await _scrape_detail(client, url, dept)
            except Exception as e:
                print(f"[DomainesForets] Erreur détail {url}: {e}")
                bien = None
            if not bien:
                await asyncio.sleep(0.5)
                continue

            # Post-filtre dept STRICT (sécurité ; déjà filtré par nom au pré-filtre)
            if bien["departement"] not in departements:
                continue

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                await asyncio.sleep(0.5)
                continue
            if prix_min and p and p < prix_min:
                await asyncio.sleep(0.5)
                continue
            if surface_min and s and s < surface_min:
                await asyncio.sleep(0.5)
                continue

            results.append(bien)
            await asyncio.sleep(0.5)

    print(f"[DomainesForets] {len(results)} biens retenus")
    return results


async def _scrape_detail(
    client: httpx.AsyncClient, url: str, dept: str
) -> dict | None:
    r = await client.get(url)
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    # Titre
    title_el = soup.select_one("h1.product_title, h1.entry-title, .product_title, h1")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    slug = url.split("/bien-immobilier/")[-1].rstrip("/")
    if not titre:
        titre = slug.replace("-", " ").strip()

    # Prix : meta WooCommerce/OpenGraph (fiable, valeur numérique)
    prix = None
    pm = soup.find("meta", property="product:price:amount") or soup.find(
        "meta", attrs={"property": "og:price:amount"}
    )
    if pm and pm.get("content"):
        prix = _to_float(pm["content"])
    if prix is None:
        pe = soup.select_one("p.price, .price .woocommerce-Price-amount bdi, "
                             ".woocommerce-Price-amount")
        if pe:
            prix = _parse_price(pe.get_text(" ", strip=True))

    # Description : meta description / og:description
    desc_el = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", property="og:description"
    )
    description = desc_el.get("content", "").strip() if desc_el else ""

    # Surface terrain = nb d'hectares mentionné dans le titre (cœur du métier)
    surface_terrain = _hectares_to_m2(titre) or _hectares_to_m2(description)

    # Code postal éventuel (rare dans le HTML brut)
    cp = ""
    m_cp = re.search(r"\b(\d{5})\b", description)
    if m_cp and m_cp.group(1)[:2] == dept:
        cp = m_cp.group(1)

    # Ville : non structurée dans la liste → None (le segment est zonal)
    ville = None

    # Type de bien depuis le titre
    type_bien = "propriété forestière"
    m_type = _TYPE_RE.search(titre)
    if m_type:
        type_bien = m_type.group(1).lower()

    # Image (og:image ; la galerie est lazy-load donc absente du HTML brut)
    photos: list[str] = []
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        src = og["content"]
        if "uploads" in src and "quadri" not in src and "logo" not in src.lower():
            photos.append(src)

    # id_annonce : réf 'DF-xxxx' si présente, sinon slug
    id_annonce = slug
    m_ref = re.search(r"DF-?(\d+)", r.text)
    if m_ref:
        id_annonce = "DF-" + m_ref.group(1)

    return {
        "source": "domaines_forets",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville,
        "code_postal": cp,
        "surface": None,  # surface bâtie rarement indiquée (biens fonciers)
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Domaines & Forêts",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _to_float(text: str) -> float | None:
    cleaned = re.sub(r"[^\d.]", "", str(text).replace(",", "."))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r",\d{2}$", "", cleaned)  # retire les centimes ',00'
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _hectares_to_m2(text: str) -> float | None:
    """'environ 45 hectares' / '37 ha' → m² (1 ha = 10 000 m²)."""
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0.,]*)\s*(?:hectares?|ha)\b", text, re.IGNORECASE)
    if not m:
        return None
    val = m.group(1).replace("\xa0", "").replace(" ", "").replace(",", ".")
    val = val.rstrip(".")
    try:
        ha = float(val)
        if 0 < ha < 100000:
            return round(ha * 10000, 0)
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
    print(f"\nTotal Domaines & Forêts: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        terr = b.get("surface_terrain")
        terr_ha = f"{terr/10000:.0f}ha" if terr else "?"
        print(
            f"  [{b['departement']}] {b['titre'][:60]}"
            f" — {b['prix']}€"
            f" — terrain {terr_ha}"
            f" — {b['type_bien']}"
        )

"""scrapers/immo_diffusion.py — Immo-Diffusion (portail multi-agences, base Loir-et-Cher)

Méthode : scrape_simple (httpx) — SSR HTML
Site : https://www.immo-diffusion.fr — portail de diffusion d'annonces alimenté par un
réseau d'agences. Historiquement implanté en Loir-et-Cher (41) ; la base reste forte
sur Vendôme / Blois / Montoire (41) et Le Mans (72), avec un stock épars ailleurs.

⚠️ Filtre département : l'URL DÉPARTEMENTALE (/fr/{region}/{dept-slug}/vente) NE filtre
PAS réellement — elle affiche une grille nationale de biens « en avant » (tous depts
confondus). Seules les URL VILLE filtrent correctement :
    /fr/{region}/{dept-slug}/{CP}/immobilier-{ville-slug}/vente
    → toutes les annonces de cette page sont dans le département de la ville.
On itère donc sur une liste de villes par département cible, et on POST-FILTRE
strictement sur code_postal[:2] (les rares cartes « featured » sans CP sont écartées).
Vérifié : aucune fuite hors-département.

Pagination : ?page=N (page 1 sans param). On s'arrête quand une page n'apporte plus
de nouvelle annonce avec CP valide.

Cartes : article.row_bien
  - URL/type : a.bmd / [data-bmdsrc]  →  /.../vente/{ID}-{type}.html
               (type ∈ maison, propriete, domaine, maison-de-village, terrain,
                appartement, villa, programme…) — on ne garde que maisons/propriétés.
  - data-title : "VENTE Maison  VENDOME (41100) 117 m2 | 178 000 €"
                 → ville, code_postal, surface, prix (source la plus fiable).
  - Réf : .compromis[title]  →  "Ref : IDxxxxx" (id_annonce).
  - Pièces / terrain : texte de la carte  →  "5 pièce(s) - 117 m2 ( Jardin 885 m2 )".
  - Description : premier <p> de la carte.
  - Photo : figure[style background-image] ou img[itemprop=image] (1 en liste ;
            galerie complétée plus tard par gallery.py).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.immo-diffusion.fr"
MAX_PAGES = 6


# Code département → (region-slug, dept-slug) tels qu'utilisés dans l'URL du portail.
DEPT_PATHS: dict[str, tuple[str, str]] = {
    "72": ("pays-de-la-loire", "sarthe"),
    "28": ("centre", "eure-et-loir"),
    "45": ("centre", "loiret"),
    "89": ("bourgogne", "yonne"),
    "49": ("pays-de-la-loire", "maine-et-loire"),
    "37": ("centre", "indre-et-loire"),
    "36": ("centre", "indre"),
    "18": ("centre", "cher"),
    "58": ("bourgogne", "nievre"),
    "41": ("centre", "loir-et-cher"),
    "53": ("pays-de-la-loire", "mayenne"),
}

# Villes sondées par département (CP, slug-ville). Le réseau étant épars, on cible
# les principales communes ; le post-filtre CP[:2] garantit 0 fuite quoi qu'il arrive.
# 41 (lane prioritaire) est le plus fourni.
DEPT_CITIES: dict[str, list[tuple[str, str]]] = {
    "41": [
        ("41100", "vendome"),
        ("41000", "blois"),
        ("41800", "montoire-sur-le-loir"),
        ("41200", "romorantin-lanthenay"),
        ("41500", "mer"),
        ("41110", "saint-aignan"),
        ("41130", "selles-sur-cher"),
        ("41700", "contres"),
        ("41400", "montrichard"),
        ("41600", "lamotte-beuvron"),
    ],
    "72": [
        ("72000", "le-mans"),
        ("72200", "la-fleche"),
        ("72300", "sable-sur-sarthe"),
        ("72100", "le-mans"),
        ("72400", "la-ferte-bernard"),
    ],
    "28": [
        ("28000", "chartres"),
        ("28100", "dreux"),
        ("28200", "chateaudun"),
        ("28400", "nogent-le-rotrou"),
    ],
    "45": [
        ("45000", "orleans"),
        ("45200", "montargis"),
        ("45300", "pithiviers"),
        ("45500", "gien"),
    ],
    "89": [
        ("89000", "auxerre"),
        ("89100", "sens"),
        ("89300", "joigny"),
        ("89200", "avallon"),
    ],
    "49": [
        ("49000", "angers"),
        ("49100", "angers"),
        ("49300", "cholet"),
        ("49400", "saumur"),
    ],
    "37": [
        ("37000", "tours"),
        ("37100", "tours"),
        ("37300", "joue-les-tours"),
        ("37400", "amboise"),
    ],
    "36": [
        ("36000", "chateauroux"),
        ("36100", "issoudun"),
        ("36200", "argenton-sur-creuse"),
    ],
    "18": [
        ("18000", "bourges"),
        ("18100", "vierzon"),
        ("18200", "saint-amand-montrond"),
    ],
    "58": [
        ("58000", "nevers"),
        ("58200", "cosne-cours-sur-loire"),
        ("58300", "decize"),
    ],
    "53": [
        ("53000", "laval"),
        ("53100", "mayenne"),
        ("53200", "chateau-gontier"),
    ],
}

# Types de bien (segment d'URL {ID}-{type}.html) à conserver / exclure.
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|maison-de-village|"
    r"maison-de-caractere|maison-de-maitre",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"programme|viager|investissement|parking",
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
            paths = DEPT_PATHS.get(dept)
            cities = DEPT_CITIES.get(dept)
            if not paths or not cities:
                continue
            region, dept_slug = paths
            dept_count = 0
            seen_ids: set[str] = set()
            for cp, ville_slug in cities:
                try:
                    biens = await _scrape_city(
                        client, dept, region, dept_slug, cp, ville_slug,
                        prix_max, prix_min, surface_min, seen_ids,
                    )
                    results.extend(biens)
                    dept_count += len(biens)
                except Exception as e:
                    print(f"[ImmoDiffusion] Erreur {dept}/{ville_slug}: {e}")
                await asyncio.sleep(0.5)
            print(f"[ImmoDiffusion] Dept {dept}: {dept_count} annonces")

    return results


async def _scrape_city(
    client: httpx.AsyncClient,
    dept: str,
    region: str,
    dept_slug: str,
    cp: str,
    ville_slug: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
    seen_ids: set[str],
) -> list[dict]:
    biens: list[dict] = []
    base = f"{BASE_URL}/fr/{region}/{dept_slug}/{cp}/immobilier-{ville_slug}/vente"

    for page in range(1, MAX_PAGES + 1):
        url = base if page == 1 else f"{base}?page={page}"
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select("article.row_bien")
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

            # POST-FILTRE STRICT : seul le département cible (le portail mélange du national)
            if not bien["code_postal"] or bien["code_postal"][:2] != dept:
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

        # Plus aucune nouvelle annonce avec CP valide → fin de la ville
        if new_on_page == 0:
            break

        await asyncio.sleep(0.4)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    # URL + type depuis [data-bmdsrc] (ou le lien h4 en secours)
    src_el = card.select_one("[data-bmdsrc]")
    href = src_el.get("data-bmdsrc", "") if src_el else ""
    if not href:
        h4a = card.select_one("h4 a")
        href = h4a.get("href", "") if h4a else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    m_type = re.search(r"/\d+-([a-z\-]+)\.html", href, re.IGNORECASE)
    type_seg = (m_type.group(1) if m_type else "").lower()
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg.replace("-", " ").strip() or "maison"

    # data-title : "VENTE Maison  VENDOME (41100) 117 m2 | 178 000 €"
    dt_el = card.select_one("[data-title]")
    data_title = dt_el.get("data-title", "") if dt_el else ""
    data_title = re.sub(r"<[^>]+>", "", data_title)  # retire <sup>2</sup>

    code_postal = ""
    m_cp = re.search(r"\((\d{5})\)", data_title)
    if m_cp:
        code_postal = m_cp.group(1)

    # Ville : entre le type et le (CP) dans le lien h4
    ville = ""
    h4a = card.select_one("h4 a")
    if h4a:
        h4txt = re.sub(r"\s+", " ", h4a.get_text(" ", strip=True))
        m_v = re.match(r"^\S+\s+(.+?)\s*\(\d{5}\)", h4txt)
        if m_v:
            ville = m_v.group(1).strip()
    if not ville and data_title:
        m_v = re.search(r"(?:VENTE\s+\w+(?:\s+[^\(]*?)?)\s+([A-ZÀ-Ÿ][\w\-' ]+?)\s*\(\d{5}\)", data_title)
        if m_v:
            ville = m_v.group(1).strip().title()

    # Prix & surface : data-title d'abord
    prix = None
    m_p = re.search(r"\|\s*([\d\s\xa0]+)\s*€", data_title)
    if m_p:
        prix = _to_float(m_p.group(1))
    surface = None
    m_s = re.search(r"(\d[\d\s\xa0]*)\s*m2", data_title)
    if m_s:
        surface = _to_float(m_s.group(1))

    full = re.sub(r"\s+", " ", card.get_text(" ", strip=True))

    # Prix en secours depuis .prix
    if prix is None:
        pr_el = card.select_one(".prix")
        if pr_el:
            prix = _to_float(pr_el.get_text(" ", strip=True))

    # Pièces : "5 pièce(s)"
    pieces = None
    m_pc = re.search(r"(\d+)\s*pi[eè]ce", full, re.IGNORECASE)
    if m_pc:
        pieces = int(m_pc.group(1))

    # Chambres : "3 chambre(s)" si présent
    chambres = None
    m_ch = re.search(r"(\d+)\s*chambre", full, re.IGNORECASE)
    if m_ch:
        chambres = int(m_ch.group(1))

    # Surface en secours depuis le texte
    if surface is None:
        m_s2 = re.search(r"(\d[\d\s\xa0]*)\s*m\s*2", full)
        if m_s2:
            surface = _to_float(m_s2.group(1))

    # Terrain / jardin : "Jardin 885 m2" ou "Terrain 2000 m2"
    surface_terrain = None
    m_t = re.search(r"(?:Jardin|Terrain)\s+([\d\s\xa0]+)\s*m\s*2", full, re.IGNORECASE)
    if m_t:
        surface_terrain = _to_float(m_t.group(1))

    # Référence : .compromis[title] = "Ref : IDxxxxx"
    id_annonce = ""
    ref_el = card.select_one(".compromis")
    if ref_el:
        ref_txt = ref_el.get_text(" ", strip=True)
        m_ref = re.search(r"ID\s*(\d+)", ref_txt, re.IGNORECASE)
        if m_ref:
            id_annonce = m_ref.group(1)
    if not id_annonce:
        m_id = re.search(r"/(\d+)-[a-z\-]+\.html", href, re.IGNORECASE)
        id_annonce = m_id.group(1) if m_id else url

    # Description : premier <p> significatif
    description = ""
    for p in card.select("p"):
        t = re.sub(r"\s+", " ", p.get_text(" ", strip=True))
        if len(t) > 40:
            description = t
            break

    # Titre
    titre = data_title.split("|")[0].strip() if data_title else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Photo (1 en liste) : figure background-image ou img[itemprop=image]
    photos: list[str] = []
    fig = card.select_one("figure")
    if fig and fig.get("style"):
        m_img = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", fig.get("style", ""))
        if m_img:
            photos.append(_abs_url(m_img.group(1)))
    if not photos:
        img = card.select_one("img[itemprop=image]") or card.select_one("img[src]")
        if img and img.get("src") and not img.get("src", "").startswith("data:"):
            photos.append(_abs_url(img.get("src")))

    return {
        "source": "immo_diffusion",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": None,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _to_float(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text or "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _abs_url(src: str) -> str:
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return BASE_URL + src
    return src


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
    print(f"\nTotal Immo-Diffusion: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['pieces'] or '?'}p — {b['type_bien']} — {b['ville']}"
        )

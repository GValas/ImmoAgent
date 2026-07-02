"""scrapers/immo_interactif.py — Immo-Interactif (enchères immobilières en ligne notariales)

Plateforme des notaires de France pour la vente interactive (enchères en ligne)
de biens immobiliers via les offices notariaux. Domaine distinct de
immobilier.notaires.fr (déjà couvert) : stock = ventes interactives uniquement.

Méthode : scrape_simple (httpx) — appli Nuxt SSR.
URL liste : /encheres-en-ligne?suggestions=D_{NN}&page={p}
            → filtre département CÔTÉ SERVEUR via le token D_{NN}.
Les cartes ne sont pas en HTML lisible (payload Nuxt hydraté), MAIS les liens des
pages détail y figurent sous la for:
    encheres-en-ligne/{type}/{ville-slug}-{NN}/{id}
→ le département est encodé dans le slug (-{NN}/), filtre fiable + 0 fuite.

Page détail : le <title> contient toutes les infos structurées :
    "Maison - 5 pièces - 77 m² - Nogent Sur Vernisson (45290) - 58,800 € | immo-interactif"
→ type, pièces, surface, ville, code postal, prix (parsés du title).
La <meta name="description"> fournit le début du descriptif.

Filtre département : serveur (D_{NN}) + slug (-{NN}/) + POST-FILTRE strict
                     code_postal[:2] == dept (0 fuite vérifié).
Stock cible : faible (quelques lots par département ; varie selon les ventes).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import html as _html
import re

import httpx

from scrapers._base import HEADERS

BASE_URL = "https://www.immo-interactif.fr"
MAX_PAGES = 5


# Types de bien (segment d'URL) à conserver : maisons / propriétés.
_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|mas|g[iî]te|corps-de-ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)

# Lien page détail dans le payload : encheres-en-ligne/{type}/{ville-NN}/{id}
_DETAIL_RE = re.compile(
    r"encheres-en-ligne/([a-z\-]+)/([a-z0-9\-]+-(\d{2,3}))/(\d+)"
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            try:
                biens = await _scrape_dept(client, dept, prix_max, surface_min)
                results.extend(biens)
                print(f"[ImmoInteractif] Dept {dept}: {len(biens)} lots")
            except Exception as e:
                print(f"[ImmoInteractif] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient, dept: str, prix_max: int, surface_min: int
) -> list[dict]:
    # 1) Collecter les liens détail (filtre serveur D_{dept})
    detail_links: dict[str, tuple[str, str]] = {}  # id -> (url, type_slug)
    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/encheres-en-ligne?suggestions=D_{dept}&page={page}"
        r = await client.get(url)
        if r.status_code != 200:
            break
        found = 0
        for m in _DETAIL_RE.finditer(r.text):
            type_slug, ville_slug, slug_dept, ann_id = m.groups()
            # filtre dept via le slug (-{NN}/)
            if slug_dept[:2] != dept:
                continue
            if ann_id in detail_links:
                continue
            path = f"encheres-en-ligne/{type_slug}/{ville_slug}/{ann_id}"
            detail_links[ann_id] = (f"{BASE_URL}/{path}", type_slug)
            found += 1
        if found == 0:
            break
        await asyncio.sleep(0.4)

    # 2) Récupérer le détail de chaque lot conservé
    biens: list[dict] = []
    for ann_id, (url, type_slug) in detail_links.items():
        # filtre type via le slug
        if _EXCLUDE_TYPE.search(type_slug) and not _KEEP_TYPE.search(type_slug):
            continue
        if not _KEEP_TYPE.search(type_slug):
            continue
        try:
            bien = await _scrape_detail(client, url, ann_id, type_slug, dept)
        except Exception:
            bien = None
        if not bien:
            continue

        # POST-FILTRE département STRICT (0 fuite)
        cp = bien.get("code_postal") or ""
        if cp and cp[:2] != dept:
            continue

        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if surface_min and s and s < surface_min:
            continue

        biens.append(bien)
        await asyncio.sleep(0.3)

    return biens


async def _scrape_detail(
    client: httpx.AsyncClient, url: str, ann_id: str, type_slug: str, dept: str
) -> dict | None:
    r = await client.get(url)
    if r.status_code != 200:
        return None
    t = r.text

    m_title = re.search(r"<title>(.*?)</title>", t, re.S)
    title = _html.unescape(m_title.group(1).strip()) if m_title else ""
    # "Maison - 5 pièces - 77 m² - Nogent Sur Vernisson (45290) - 58,800 € | ..."
    title_main = title.split("|")[0].strip()

    type_bien = type_slug.replace("-", " ").strip() or "maison"

    pieces = None
    m_p = re.search(r"(\d+)\s*pi[eè]ces?", title_main, re.IGNORECASE)
    if m_p:
        pieces = int(m_p.group(1))

    surface = None
    m_s = re.search(r"(\d+(?:[.,]\d+)?)\s*m[²2]", title_main, re.IGNORECASE)
    if m_s:
        try:
            surface = float(m_s.group(1).replace(",", "."))
        except ValueError:
            surface = None

    ville, code_postal = "", ""
    m_loc = re.search(r"-\s*([^()\-]+?)\s*\((\d{5})\)", title_main)
    if m_loc:
        ville = m_loc.group(1).strip()
        code_postal = m_loc.group(2)

    prix = None
    m_pr = re.search(r"([\d\s\xa0.,]+)\s*€", title_main)
    if m_pr:
        prix = _parse_price(m_pr.group(1))

    # Description (meta)
    description = ""
    m_d = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{0,400})',
        t,
    )
    if m_d:
        description = _html.unescape(m_d.group(1)).strip()

    # Photos (og:image ou media.immo-interactif)
    photos = []
    for m in re.findall(r"https://media\.immo-interactif\.fr/[^\"'\s)]+", t):
        if m not in photos:
            photos.append(m)
    photos = photos[:8]

    if not (code_postal or ville):
        return None

    return {
        "source": "immo_interactif",
        "url": url,
        "id_annonce": ann_id,
        "titre": (title_main or f"{type_bien.title()} {ville}")[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Notaires (Immo-Interactif)",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    """'58,800 €' / '385 800' → float. La virgule peut être séparateur de millier."""
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[.,](?=\d{3}\b)", "", cleaned)  # millier
    cleaned = re.sub(r"[^\d]", "", cleaned)
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
    print(f"\nTotal Immo-Interactif: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

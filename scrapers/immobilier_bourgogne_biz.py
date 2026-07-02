"""scrapers/immobilier_bourgogne_biz.py — Immobilier Bourgogne (.biz, réseau Le Lys)

Méthode : scrape_simple (httpx) — SSR HTML statique (.htm), aucun JS requis.

⚠️ NE PAS confondre avec immobilier-bourgogne.NET (déjà en sources.yaml, actif:false,
   agence CADR'IMMO Dijon / Côte-d'Or 21). Ici c'est le .biz, portail régional du
   réseau "Le Lys Téméraire / demeure.biz" (demeures de caractère : châteaux, moulins,
   manoirs, domaines) couvrant la Bourgogne — surtout Yonne (89) et Nièvre (58).

Architecture du réseau :
  - immobilier-bourgogne.biz/index.htm = page d'atterrissage régionale (quelques fiches
    en avant + liens vers les pages de listing géographiques du hub demeure.biz).
  - demeure.biz/{NN-nom}.htm = pages de listing par zone géographique
    (ex: 89-yonne-puisaye.htm, 58-nievre-nivernais.htm, 18-cher-berry.htm).
  - Les fiches détail vivent sur des SOUS-DOMAINES thématiques du réseau
    (moulin.lys-temeraire.com, demeure.lys-temeraire.com, manoir-a-vendre.biz…),
    avec un slug d'URL préfixé par le ou les CODES DÉPARTEMENT :
    ex /89-puisaye-saint-sauveur-...htm, /58-nievre-...htm, /03-58-allier-nievre-...htm

Filtre département : il n'y a PAS de code postal sur les fiches (seulement le n° de
  département + le nom de commune). On extrait le(s) code(s) dept du PRÉFIXE du slug
  d'URL ('89-...', '03-58-...') et on POST-FILTRE strictement : on ne garde une fiche
  que si l'un de ses codes préfixes est dans la zone cible ; le `departement` retourné
  est ce code en-zone. code_postal laissé à None (absent de la source).

Fiche détail (SSR) :
  - Titre        : <h1> (ou <title>)
  - Prix         : <h3>Prix : 250 000 €</h3>
  - Surface/pcs  : déduits du titre/H1 ("230 m²", "6 pièces")
  - Description  : meta[name=description]
  - Photos       : og:image + images relatives du même préfixe (résolues sur le domaine)
  - CP/DPE       : non exposés → None

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

# Page d'atterrissage régionale + pages de listing géographiques du hub (zone cible).
LANDING = "https://immobilier-bourgogne.biz/index.htm"
LISTING_PAGES = [
    LANDING,
    "https://demeure.biz/89-yonne-puisaye.htm",
    "https://demeure.biz/58-nievre-nivernais.htm",
    "https://demeure.biz/18-cher-berry.htm",
    "https://demeure.biz/biens-immobiliers-nouveaux-a-la-vente.htm",
    "https://demeure.biz/biens-immobiliers-par-lieux.htm",
]

MAX_FICHES = 60
PHOTOS_PER_CARD = 8


# Slug d'URL d'une fiche : commence par "NN-" (ou "NN-NN-…" multi-dept).
_FICHE_RE = re.compile(r"/(\d{2}(?:-\d{2})*)-[a-z0-9-]+\.htm$", re.IGNORECASE)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    target = set(departements)
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_urls: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        # 1) Collecter toutes les URLs de fiches dont le préfixe dept touche la zone.
        candidates: list[tuple[str, str]] = []  # (url, dept_en_zone)
        for page in LISTING_PAGES:
            try:
                links = await _collect_fiches(client, page)
            except Exception as e:
                print(f"[ImmoBourgogneBiz] Erreur listing {page}: {e}")
                continue
            for url in links:
                if url in seen_urls:
                    continue
                dept = _dept_in_zone(url, target)
                if dept is None:
                    continue
                seen_urls.add(url)
                candidates.append((url, dept))
            await asyncio.sleep(0.5)

        print(f"[ImmoBourgogneBiz] {len(candidates)} fiche(s) en zone à visiter")

        # 2) Visiter chaque fiche détail (un GET poli par fiche).
        for url, dept in candidates[:MAX_FICHES]:
            try:
                bien = await _parse_fiche(client, url, dept)
            except Exception as e:
                print(f"[ImmoBourgogneBiz] Erreur fiche {url}: {e}")
                bien = None
            if not bien:
                continue

            # Post-filtre dept STRICT (le dept vient du slug, déjà vérifié en zone).
            if bien["departement"] not in target:
                continue

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            results.append(bien)
            await asyncio.sleep(0.5)

    print(f"[ImmoBourgogneBiz] {len(results)} annonce(s) retenue(s)")
    return results


async def _collect_fiches(client: httpx.AsyncClient, page_url: str) -> list[str]:
    r = await client.get(page_url)
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    urls: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if _FICHE_RE.search(href):
            full = href if href.startswith("http") else _resolve(page_url, href)
            urls.append(full)
    return urls


def _dept_in_zone(url: str, target: set[str]) -> str | None:
    """Renvoie le 1er code dept du préfixe de slug qui est dans la zone, sinon None."""
    m = _FICHE_RE.search(url)
    if not m:
        return None
    for code in m.group(1).split("-"):
        if code in target:
            return code
    return None


async def _parse_fiche(client: httpx.AsyncClient, url: str, dept: str) -> dict | None:
    r = await client.get(url)
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    # Titre
    h1 = soup.find("h1")
    titre = h1.get_text(" ", strip=True) if h1 else ""
    if not titre and soup.title:
        titre = soup.title.get_text(" ", strip=True)
    if not titre:
        return None

    # Le titre <title> est souvent plus riche en localisation que <h1>.
    title_tag = soup.title.get_text(" ", strip=True) if soup.title else ""

    # Description
    description = ""
    md = soup.find("meta", attrs={"name": "description"})
    if md and md.get("content"):
        description = md["content"].strip()

    # Prix : <h3>Prix : 250 000 €</h3>
    prix = None
    for h3 in soup.find_all("h3"):
        t = h3.get_text(" ", strip=True)
        if "€" in t or re.search(r"prix", t, re.IGNORECASE):
            prix = _parse_price(t)
            if prix:
                break
    if prix is None:
        prix = _parse_price_labeled(soup.get_text(" ", strip=True))

    # Surface / pièces depuis le titre (H1 + <title>)
    blob = f"{titre} {title_tag}"
    surface = _parse_surface(blob)
    pieces = _parse_pieces(blob)

    # Type de bien (mot-clé dominant)
    type_bien = _detect_type(f"{titre} {url}")

    # Ville : extraite du <title> ("… proche Saint-Sauveur-en-Puisaye, Yonne (89) …"),
    # à défaut le 2e segment du slug d'URL ("89-chablis-…" → "Chablis").
    ville = _parse_ville(title_tag) or _parse_ville(titre) or _ville_from_slug(url)

    # Photos : og:image + images relatives du même préfixe que la fiche.
    photos = _collect_photos(soup, url)

    return {
        "source": "immobilier_bourgogne_biz",
        "url": url,
        "id_annonce": _slug_id(url),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": (ville or "")[:80],
        "code_postal": None,  # absent de la source
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Le Lys Téméraire (réseau demeure.biz)",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resolve(base_url: str, href: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base_url, href)


def _slug_id(url: str) -> str:
    slug = url.rsplit("/", 1)[-1]
    return re.sub(r"\.htm$", "", slug)[:120]


def _parse_price(text: str) -> float | None:
    m = re.search(r"([\d  \.]{4,})\s*(?:€|euros)", text, re.IGNORECASE)
    if not m:
        return None
    cleaned = re.sub(r"[^\d]", "", m.group(1))
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    if v and 10000 <= v <= 50_000_000:
        return v
    return None


def _parse_price_labeled(text: str) -> float | None:
    m = re.search(r"Prix\s*:?\s*([\d  \.]{4,})\s*(?:€|euros)", text, re.IGNORECASE)
    if m:
        cleaned = re.sub(r"[^\d]", "", m.group(1))
        try:
            v = float(cleaned)
            if 10000 <= v <= 50_000_000:
                return v
        except ValueError:
            pass
    return None


def _parse_surface(text: str) -> float | None:
    # Cherche "NNN m²" (habitable) ; ignore les surfaces de terrain en ha.
    for m in re.finditer(r"(\d[\d  ]{0,4})\s*m²", text):
        val = re.sub(r"[  ]", "", m.group(1))
        try:
            f = float(val)
        except ValueError:
            continue
        if 20 <= f <= 5000:
            return f
    return None


def _parse_pieces(text: str) -> int | None:
    m = re.search(r"(\d+)\s*pi[eè]ce", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_ville(text: str) -> str | None:
    """'… proche Saint-Sauveur-en-Puisaye, Yonne (89) …' → 'Saint-Sauveur-en-Puisaye'."""
    if not text:
        return None
    m = re.search(
        r"(?:proche|à|a)\s+([A-ZÀ-Ý][\w'\-]+(?:[ -][A-ZÀ-Ýa-zà-ÿ'][\w'\-]+){0,4})"
        r"\s*,?\s*(?:Yonne|Ni[eè]vre|Cher|C[oô]te-d'Or|Sa[oô]ne)",
        text,
    )
    if m:
        return m.group(1).strip(" ,")
    return None


_SLUG_NOISE = {
    "nievre", "yonne", "cher", "berry", "puisaye", "allier", "bourgogne",
    "nivernais", "a", "vendre",
}


def _ville_from_slug(url: str) -> str | None:
    """'89-chablis-moulin-a-vendre-…' → 'Chablis' (2e segment, hors mots de zone)."""
    slug = re.sub(r"\.htm$", "", url.rsplit("/", 1)[-1])
    parts = slug.split("-")
    # Sauter le(s) code(s) dept en tête.
    i = 0
    while i < len(parts) and re.fullmatch(r"\d{2}", parts[i]):
        i += 1
    if i < len(parts) and parts[i].lower() not in _SLUG_NOISE:
        return parts[i].capitalize()
    return None


def _detect_type(text: str) -> str:
    t = text.lower()
    for kw, label in [
        ("château", "château"), ("chateau", "château"),
        ("moulin", "moulin"), ("manoir", "manoir"),
        ("abbaye", "abbaye"), ("domaine", "domaine"),
        ("ferme", "ferme"), ("longère", "longère"),
        ("propriété", "propriété"), ("propriete", "propriété"),
        ("demeure", "demeure"), ("maison", "maison"),
    ]:
        if kw in t:
            return label
    return "propriété"


def _collect_photos(soup, url: str) -> list[str]:
    photos: list[str] = []
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        photos.append(_resolve(url, og["content"]))

    # Préfixe d'images de la fiche, ex N630 → ne garder que les images N630*.
    prefix = None
    if photos:
        m = re.search(r"/(N\d+)", photos[0])
        if m:
            prefix = m.group(1)

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:") or src.lower().endswith(".png"):
            continue
        full = src if src.startswith("http") else _resolve(url, src)
        if prefix and prefix not in full:
            continue
        if full not in photos:
            photos.append(full)
    return photos


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
    print(f"\nTotal Immobilier Bourgogne (.biz): {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville'] or '?'}"
        )

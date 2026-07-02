"""scrapers/costes_viager.py — Renée Costes Viager (réseau national viager / nue-propriété)

Méthode : scrape_simple (httpx) — SSR Angular (les annonces sont dans le HTML brut).
URL pattern : /acheter/annonces/departement/{nom-dept}-{DD}
              (ex: /acheter/annonces/departement/loiret-45, /sarthe-72, /maine-et-loire-49)
              → filtre département CÔTÉ SERVEUR. Le code dept figure aussi dans
              chaque URL détail : /acheter/{nom-DD}/{ville}/{type-Npieces-ID}.

Cartes : rc-card-annonce  (web-component Angular, rendu côté serveur)
  - URL    : a.container[href]  → /acheter/{slug-DD}/{ville}/{type}-{N}pieces-{ID}
  - Type+pieces+surface : h5  →  "Maison - 6 pièces - 136m²"
  - Statut : h4  →  "Viager occupé" / "Viager libre" / "Nue-propriété"
  - Réf    : rc-tag "Ref. : 422645031"
  - Loc    : rc-tag[icon=map]  →  "proche de Montargis (45200)"
  - Prix   : texte de la carte  →  "Bouquet FAI 48 810 €", "Valeur du bien 198 000 €",
             "Prix d'achat 108 504 €". On retient le « Prix d'achat » (montant réellement
             déboursé par l'acquéreur d'un viager) ; repli sur le bouquet, puis la valeur.
  - Photos : img.annonce[src]  (img.costes-viager.com/...)

Pagination : la liste est paginée côté client (scroll/API non-SSR) ; le HTML SSR
             renvoie la 1ʳᵉ page (~18 cartes/dept). On scrape donc cette 1ʳᵉ page
             par département (volume raisonnable, stock réel).

Filtre dept : slug serveur + post-filtre STRICT code_postal[:2] == dept (0 fuite).

Particularité : produit de niche (viager / nue-propriété). type_bien dérivé de h5.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int

BASE_URL = "https://www.costes-viager.com"
PHOTOS_PER_CARD = 10


# Code département → slug URL costes-viager.com/acheter/annonces/departement/{slug}
DEPT_SLUGS: dict[str, str] = {
    "72": "sarthe-72",
    "28": "eure-et-loir-28",
    "45": "loiret-45",
    "89": "yonne-89",
    "49": "maine-et-loire-49",
    "37": "indre-et-loire-37",
    "36": "indre-36",
    "18": "cher-18",
    "58": "nievre-58",
    "41": "loir-et-cher-41",
    "53": "mayenne-53",
}

# Types de bien (depuis h5) à conserver : maisons / propriétés. Exclut appartements.
_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|local|commerce|garage|parking|immeuble|bureau|terrain",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, slug, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[CostesViager] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[CostesViager] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    slug: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    url = f"{BASE_URL}/acheter/annonces/departement/{slug}"
    r = None
    for attempt in range(3):
        try:
            r = await client.get(url)
            break
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
            if attempt == 2:
                raise
            await asyncio.sleep(1.5)
    if r is None or r.status_code != 200:
        return biens

    cards = BeautifulSoup(r.text, "html.parser").select("rc-card-annonce")
    for card in cards:
        try:
            bien = _parse_card(card, dept)
        except Exception:
            continue
        if not bien:
            continue

        # Post-filtre STRICT : on n'accepte que le département cible (0 fuite)
        if bien["code_postal"] and bien["code_postal"][:2] != dept:
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

    return biens


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a.container") or card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href or "/acheter/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    full_text = card.get_text(" ", strip=True)

    # Type / pièces / surface depuis h5 : "Maison - 6 pièces - 136m²"
    h5 = card.select_one("h5")
    h5_text = h5.get_text(" ", strip=True) if h5 else ""
    if _EXCLUDE_TYPE.search(h5_text):
        return None
    type_bien = _parse_type(h5_text)
    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", h5_text)
    surface = _parse_surface(h5_text)

    # Statut viager (h4) → préfixe au titre
    h4 = card.select_one("h4")
    statut = h4.get_text(" ", strip=True) if h4 else ""

    # Localisation : rc-tag[icon=map] → "proche de Montargis (45200)"
    loc_text = ""
    map_tag = card.select_one("rc-tag[icon=map]")
    if map_tag:
        loc_text = map_tag.get_text(" ", strip=True)
    ville, code_postal = _parse_loc(loc_text)
    # repli : CP via l'URL détail (slug-DD) ou texte
    if not code_postal:
        m = re.search(r"\((\d{5})\)", full_text)
        if m:
            code_postal = m.group(1)

    # Référence (id_annonce) — dans l'URL détail (dernier segment) ou "Ref. : NNN"
    id_annonce = ""
    m_id = re.search(r"-(\d{6,})/?$", href.rstrip("/"))
    if m_id:
        id_annonce = m_id.group(1)
    if not id_annonce:
        m_ref = re.search(r"Ref\.?\s*:?\s*(\d{4,})", full_text)
        id_annonce = m_ref.group(1) if m_ref else url

    # Prix : « Prix d'achat » prioritaire, puis bouquet (FAI), puis valeur du bien
    prix = (
        _parse_amount(r"Prix d['’]achat\s*([\d\s\xa0]+)\s*€", full_text)
        or _parse_amount(r"Bouquet(?:\s*FAI)?\s*([\d\s\xa0]+)\s*€", full_text)
        or _parse_amount(r"Valeur du bien\s*([\d\s\xa0]+)\s*€", full_text)
    )

    # Titre
    titre = " - ".join(x for x in [statut, h5_text] if x).strip()
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Photos
    photos = []
    for img in card.select("img.annonce"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "costes_viager",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": full_text[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Renée Costes Viager",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_type(h5_text: str) -> str:
    """'Maison - 6 pièces - 136m²' → 'maison'."""
    if not h5_text:
        return "maison"
    seg = h5_text.split("-")[0].strip().lower()
    return seg or "maison"


def _parse_loc(text: str) -> tuple[str, str]:
    """'proche de Montargis (45200)' → ('Montargis', '45200')."""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    ville = re.sub(r"^proche de\s+", "", ville, flags=re.IGNORECASE).strip()
    return ville, cp


def _parse_amount(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
        return f if f > 0 else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'... - 136m²' → 136.0."""
    m = re.search(r"(\d[\d\s\xa0]*)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 3000:
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
    print(f"\nTotal Costes Viager: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

"""scrapers/vench.py — Vench (ventes aux enchères immobilières judiciaires)

Méthode : scrape_simple (httpx) — SSR HTML (status 200, pas de Cloudflare).

URL pattern : /ventes-aux-encheres-par-departement-{Nom}.html
              (ex: ...-Loiret.html, ...-Sarthe.html, ...-Yonne.html)
              → filtre département CÔTÉ SERVEUR par slug nom français.
              Une seule page par département (pas de pagination observée).

Cartes : div.featured-item
  - URL    : a[href*="vente-"]  → ./vente-{ID}-{slug}.html
  - Titre  : h3  →  "UNE MAISON D'HABITATION • 137m² • Champigny"
             (format "TYPE • [SURFACEm² •] Ville" ; la surface est optionnelle)
  - Prix   : .miseAPrixVignette  →  "Mise à prix : 60 000.00 €"
  - Photo  : img.imgVignetteVente[data-src]
  - Date   : .dateVenteVignette

Le code postal n'est PAS dans la carte de liste : on le récupère sur la
page détail (bloc <p class="h4">Adresse</p> suivi d'un <p> "{CP} {Ville}").
Cela permet un post-filtre STRICT code_postal[:2] == dept (0 fuite).

Type de bien : déduit du titre. On ne garde que maisons / propriétés / fermes
               (on exclut terrains, ensembles pro, locaux, garages...).

Particularité : le descriptif complet est réservé aux abonnés ; on n'a que le
                titre + l'adresse en clair. Prix = mise à prix de l'enchère.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.vench.fr"
PHOTOS_PER_CARD = 3
DETAIL_CONCURRENCY = 6


# Code département → slug nom français de l'URL Vench
DEPT_SLUGS: dict[str, str] = {
    "72": "Sarthe",
    "28": "Eure-et-Loir",
    "45": "Loiret",
    "89": "Yonne",
    "49": "Maine-et-Loire",
    "37": "Indre-et-Loire",
    "36": "Indre",
    "18": "Cher",
    "58": "Nievre",  # Nièvre sans accent dans le slug
    "41": "Loir-et-Cher",
    "53": "Mayenne",
}

# Types de bien (depuis le titre) à conserver : maisons / propriétés / fermes...
_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|pavillon|corps\s+de\s+ferme|habitation",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"terrain|local|commerc|garage|parking|bureau|fonds|appartement|"
    r"ensemble\s+immobilier|usage\s+professionnel|industriel|hangar|entrep[oô]t",
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
                print(f"[Vench] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Vench] Erreur dept {dept}: {e}")
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
    url = f"{BASE_URL}/ventes-aux-encheres-par-departement-{slug}.html"
    r = await client.get(url)
    if r.status_code != 200:
        print(f"[Vench] Dept {dept}: status {r.status_code}")
        return []

    cards = BeautifulSoup(r.text, "html.parser").select("div.featured-item")
    candidats: list[dict] = []
    seen_ids: set[str] = set()

    for card in cards:
        try:
            bien = _parse_card(card, dept)
        except Exception:
            continue
        if not bien:
            continue

        aid = bien["id_annonce"]
        if aid in seen_ids:
            continue

        # Filtres connus dès la liste (prix = mise à prix ; surface si présente)
        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        seen_ids.add(aid)
        candidats.append(bien)

    # Récupère le code postal sur les pages détail (post-filtre STRICT)
    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def enrich(bien: dict) -> dict | None:
        async with sem:
            cp, ville = await _fetch_cp(client, bien["url"])
        if cp:
            bien["code_postal"] = cp
            if ville:
                bien["ville"] = ville[:80]
        # Post-filtre département STRICT : 0 fuite hors-zone
        if not cp or cp[:2] != dept:
            return None
        return bien

    enriched = await asyncio.gather(*(enrich(b) for b in candidats))
    return [b for b in enriched if b]


async def _fetch_cp(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """Récupère (code_postal, ville) depuis la page détail.

    Bloc cible : <p class="h4">Adresse</p> suivi d'un <p> "{CP} {Ville}".
    """
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return "", ""
        soup = BeautifulSoup(r.text, "html.parser")
        lab = soup.find("p", string=re.compile(r"^\s*Adresse\s*$", re.IGNORECASE))
        if lab:
            nxt = lab.find_next("p")
            if nxt:
                txt = nxt.get_text(" ", strip=True)
                m = re.search(r"\b(\d{5})\b\s*(.*)", txt)
                if m:
                    return m.group(1), m.group(2).strip()
        # Repli : premier CP du bloc adresse dans le HTML
        m = re.search(r'class="h4">\s*Adresse\s*</p>.*?\b(\d{5})\b', r.text, re.S)
        if m:
            return m.group(1), ""
    except Exception:
        pass
    return "", ""


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one('a[href*="vente-"]')
    href = link.get("href", "") if link else ""
    if not href:
        return None
    href_clean = href.lstrip(".")
    url = href_clean if href_clean.startswith("http") else BASE_URL + (
        href_clean if href_clean.startswith("/") else "/" + href_clean
    )

    m_id = re.search(r"/vente-(\d+)-", url)
    id_annonce = m_id.group(1) if m_id else url

    h3 = card.select_one("h3")
    raw_title = h3.get_text(" ", strip=True) if h3 else ""
    raw_title = re.sub(r"\s+", " ", raw_title).strip()

    # Format "TYPE • [SURFACEm² •] Ville" séparé par puces "•"
    segments = [s.strip() for s in raw_title.split("•") if s.strip()]
    type_part = segments[0] if segments else raw_title
    ville = segments[-1] if len(segments) >= 2 else ""
    surface = None
    for seg in segments[1:-1] + ([segments[-1]] if len(segments) >= 2 else []):
        ms = re.search(r"([\d]+(?:[.,]\d+)?)\s*m²", seg)
        if ms:
            try:
                surface = float(ms.group(1).replace(",", "."))
            except ValueError:
                surface = None
            break

    # Type de bien (filtrage maisons / propriétés)
    if _EXCLUDE_TYPE.search(type_part) and not _KEEP_TYPE.search(type_part):
        return None
    if not _KEEP_TYPE.search(type_part):
        return None
    type_bien = _type_label(type_part)

    # Prix = mise à prix
    mp = card.select_one(".miseAPrixVignette")
    prix = _parse_price(mp.get_text(" ", strip=True) if mp else "")

    # Date de la vente
    dv = card.select_one(".dateVenteVignette")
    date_vente = ""
    if dv:
        md = re.search(r"(\d{2}/\d{2}/\d{2,4})", dv.get_text(" ", strip=True))
        if md:
            date_vente = md.group(1)

    # Photo (vignette)
    photos = []
    img = card.select_one("img.imgVignetteVente")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    titre = raw_title[:150] or f"{type_bien} {ville}".strip()
    description = (
        f"Vente aux enchères judiciaire — {raw_title}"
        + (f" — Vente le {date_vente}" if date_vente else "")
    ).strip()

    return {
        "source": "vench",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre,
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",  # rempli depuis la page détail
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Vench (vente aux enchères)",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _type_label(text: str) -> str:
    t = text.lower()
    if re.search(r"ch[aâ]teau", t):
        return "château"
    if re.search(r"propri[eé]t[eé]", t):
        return "propriété"
    if re.search(r"ferme|long[eè]re|corps\s+de\s+ferme", t):
        return "ferme"
    if re.search(r"manoir|demeure|domaine", t):
        return "demeure"
    if re.search(r"villa|pavillon", t):
        return "maison"
    return "maison"


def _parse_price(text: str) -> float | None:
    """'Mise à prix : 60 000.00 €' → 60000.0"""
    m = re.search(r"([\d\s\xa0]+(?:[.,]\d+)?)\s*€", text)
    if not m:
        return None
    raw = m.group(1)
    raw = re.sub(r"[\s\xa0]", "", raw)
    # format "60000.00" → point décimal ; on retire les décimales .NN
    raw = re.sub(r"\.(\d{2})$", "", raw)
    raw = raw.replace(",", "")
    try:
        return float(raw) if raw else None
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
    print(f"\nTotal Vench: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

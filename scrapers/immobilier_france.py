"""scrapers/immobilier_france.py — Immobilier-France.fr (agrégateur national)

Méthode : scrape_simple (httpx) — SSR
Agrégateur de biens (sources bien'ici, Leggett, agences…) — annonces de vente.

URL pattern liste : /maison-{slug}?page=N   (ex: /maison-loiret, /maison-sarthe)
  → filtre département CÔTÉ SERVEUR via le slug, MAIS le slug FUITE
    (quelques biens hors-département observés : 91, 77, 13…) → post-filtre STRICT
    obligatoire sur code_postal[:2].

Liste : bloc JSON-LD `application/ld+json` unique
  CollectionPage > mainEntity (ItemList) > itemListElement[] de RealEstateListing
  Chaque item : name (titre), url (= page détail /search/{ref}), description,
                image (1 photo), offers.price.
  ⚠ Le code postal n'est PAS dans la liste. Il faut ouvrir la page détail et lire
    le `<h1>` : "Orléans (45100) • Maison" → ville, code_postal, type.
  ⚠ La "région" du descriptif est NON fiable (ex. Égreville=77 affiché en Loiret)
    → on ne s'y fie jamais pour le département.

Détail (/search/{ref}) : `<h1 class="...">Ville (CODEPOSTAL) • Type</h1>`
  C'est la seule source fiable du CP → indispensable pour le post-filtre.

Surface / pièces : extraites du titre ("Maison 5 pièces 154 m²").

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.immobilier-france.fr"
MAX_PAGES = 6
DETAIL_CONCURRENCY = 6


# Code département → slug d'URL /maison-{slug}
DEPT_SLUGS: dict[str, str] = {
    "72": "sarthe",
    "28": "eure-et-loir",
    "45": "loiret",
    "89": "yonne",
    "49": "maine-et-loire",
    "37": "indre-et-loire",
    "36": "indre",
    "18": "cher",
    "58": "nievre",
    "41": "loir-et-cher",
    "53": "mayenne",
}

# Types de bien à conserver (déduits du titre / type du H1)
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|corps de ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
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
                print(f"[ImmoFrance] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[ImmoFrance] Erreur dept {dept}: {e}")
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
    # 1) Collecte des items (liste, rapide)
    raw_items: list[dict] = []
    seen_urls: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/maison-{slug}?page={page}"
        r = await client.get(url)
        if r.status_code != 200:
            break
        items = _extract_listings(r.text)
        if not items:
            break

        new_on_page = 0
        for it in items:
            u = it.get("url", "")
            if not u or u in seen_urls:
                continue
            seen_urls.add(u)
            raw_items.append(it)
            new_on_page += 1

        if new_on_page == 0:
            break
        await asyncio.sleep(0.5)

    # 2) Pré-filtre prix/surface depuis la liste (avant les requêtes détail coûteuses)
    candidates: list[dict] = []
    for it in raw_items:
        prix = _parse_price(it.get("offers", {}).get("price"))
        surface, pieces = _parse_title(it.get("name", ""))
        if prix_max and prix and prix > prix_max:
            continue
        if prix_min and prix and prix < prix_min:
            continue
        if surface_min and surface and surface < surface_min:
            continue
        # type : exclure appartement/terrain si déduit du titre
        title = it.get("name", "")
        if _EXCLUDE_TYPE.search(title) and not _KEEP_TYPE.search(title):
            continue
        it["_prix"] = prix
        it["_surface"] = surface
        it["_pieces"] = pieces
        candidates.append(it)

    # 3) Page détail pour récupérer le CP fiable → post-filtre STRICT département
    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def _enrich(it: dict) -> dict | None:
        async with sem:
            try:
                rd = await client.get(it["url"])
            except Exception:
                return None
        if rd.status_code != 200:
            return None
        ville, cp, type_h1 = _parse_h1(rd.text)
        # POST-FILTRE STRICT : on rejette toute fuite hors-département
        if not cp or cp[:2] != dept:
            return None
        # Exclure les types non maison déduits du H1
        if type_h1 and _EXCLUDE_TYPE.search(type_h1) and not _KEEP_TYPE.search(type_h1):
            return None
        return _build_bien(it, dept, ville, cp, type_h1, rd.text)

    enriched = await asyncio.gather(*[_enrich(it) for it in candidates])
    return [b for b in enriched if b]


# ── Extraction ────────────────────────────────────────────────────────────────

def _extract_listings(html: str) -> list[dict]:
    """Retourne la liste des RealEstateListing du bloc JSON-LD CollectionPage."""
    for block in re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    ):
        try:
            data = json.loads(block)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        main = data.get("mainEntity")
        if isinstance(main, dict) and main.get("@type") == "ItemList":
            items = main.get("itemListElement", [])
            return [it for it in items if isinstance(it, dict)]
    return []


def _build_bien(
    it: dict, dept: str, ville: str, cp: str, type_h1: str, detail_html: str
) -> dict:
    url = it.get("url", "")
    ref = url.rstrip("/").split("/")[-1] if url else ""
    titre = (it.get("name") or "").strip()
    description = (it.get("description") or "").strip()

    type_bien = (type_h1 or "").strip().lower() or "maison"
    if not _KEEP_TYPE.search(type_bien):
        # le H1 n'a pas donné de type exploitable → fallback titre
        type_bien = "maison" if _KEEP_TYPE.search(titre) else type_bien

    photos = _collect_photos(it, detail_html)

    return {
        "source": "immobilier_france",
        "url": url,
        "id_annonce": ref or url,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": it.get("_surface"),
        "surface_terrain": _parse_terrain(description),
        "pieces": it.get("_pieces"),
        "chambres": _parse_chambres(description),
        "prix": it.get("_prix"),
        "photos": photos,
        "dpe": _parse_dpe(description),
        "agence": "Immobilier-France.fr",
    }


def _collect_photos(it: dict, detail_html: str) -> list[str]:
    photos: list[str] = []
    img = it.get("image")
    if isinstance(img, str) and img:
        photos.append(img)
    # photos supplémentaires depuis le détail (balises <img> bucket/file)
    for m in re.findall(
        r'src="(https://(?:bucket|file)\.immobilier-france\.fr/[^"]+)"', detail_html
    ):
        if m not in photos:
            photos.append(m)
        if len(photos) >= 10:
            break
    return photos[:10]


# ── Parsing helpers ─────────────────────────────────────────────────────────────

def _parse_h1(html: str) -> tuple[str, str, str]:
    """'<h1>Orléans (45100) • Maison</h1>' → ('Orléans', '45100', 'Maison')."""
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    txt = h1.get_text(" ", strip=True) if h1 else ""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", txt)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\).*$", "", txt).strip()
    type_part = ""
    if "•" in txt:
        type_part = txt.split("•", 1)[1].strip()
    return ville, cp, type_part


def _parse_title(title: str) -> tuple[float | None, int | None]:
    """'Maison 5 pièces 154 m²' → (154.0, 5)."""
    surface = None
    pieces = None
    m_s = re.search(r"([\d\s\xa0]+)\s*m²", title)
    if m_s:
        val = re.sub(r"[\s\xa0]", "", m_s.group(1))
        try:
            f = float(val)
            if 8 <= f <= 5000:
                surface = f
        except ValueError:
            pass
    m_p = re.search(r"(\d+)\s*pi[eè]ces?", title, re.IGNORECASE)
    if m_p:
        pieces = int(m_p.group(1))
    return surface, pieces


def _parse_price(raw) -> float | None:
    if raw is None:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(raw))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_terrain(text: str) -> float | None:
    if not text:
        return None
    m = re.search(
        r"terrain[^0-9]{0,20}([\d\s\xa0]+)\s*m²", text, re.IGNORECASE
    )
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 0 < f <= 1_000_000:
                return f
        except ValueError:
            pass
    return None


def _parse_chambres(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d+)\s*(?:grandes?\s+)?chambres?", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_dpe(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"DPE\s+de\s+([A-G])\b", text, re.IGNORECASE)
    return m.group(1).upper() if m else None


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
    print(f"\nTotal Immobilier-France: {len(biens)} annonces")
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

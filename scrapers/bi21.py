"""scrapers/bi21.py — Bourse Immobilière 21 (BI21)

Méthode : scrape_simple (httpx) — SSR HTML attendu.
État : INACTIF (actif: false dans sources.yaml) — BLACKLIST.

⚠️  Cloudflare managed challenge sur TOUTES les pages (testé 2026-05-30) :
    - HTTP 403 systématique, en-tête `cf-mitigated: challenge`
    - corps = interstitiel "Just a moment..." + `window._cf_chl_opt` (turnstile)
    - testé sur /, /vente+immobilier.html, /recherche.html, apex + www,
      avec UA Chrome ET Safari réalistes → toujours 403.
    → Infranchissable en httpx pur. Nécessiterait un proxy résidentiel rotatif
      ou un cookie de session cf_clearance réel (cf. PAP, Logic-Immo, OuestFrance).

Pertinence métier limitée de toute façon : réseau ~75 agences centré
Côte-d'Or / Bourgogne (dept 21), hors de la zone cible (Sarthe + couronne
Centre/Val-de-Loire/Pays-de-la-Loire). Seuls 89 (Yonne) et 58 (Nièvre) sont
limitrophes ; stock attendu très faible voire nul sur les depts cibles.

Le code ci-dessous est un parseur SSR best-effort (sélecteurs à confirmer le
jour où l'accès Cloudflare est contourné). Tant que le challenge est actif,
`search()` renvoie [] proprement.

Pattern d'URL supposé : /vente+immobilier.html (listing national, pagination
à confirmer) → post-filtre par code_postal[:2] in departements (le site ne
propose pas de filtre département serveur par slug d'URL connu).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.bi21.com"
LISTING_URL = f"{BASE_URL}/vente+immobilier.html"
MAX_PAGES = 30
PHOTOS_PER_CARD = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}

_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|corps de ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|terrain\b|local|commerce|garage|parking|immeuble|"
    r"bureau|fonds",
    re.IGNORECASE,
)


def _is_cloudflare_challenge(resp: httpx.Response) -> bool:
    if resp.headers.get("cf-mitigated") == "challenge":
        return True
    body = resp.text[:2000]
    return "Just a moment" in body or "_cf_chl_opt" in body


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    results: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = LISTING_URL if page == 1 else f"{LISTING_URL}?page={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[BI21] Erreur page {page}: {e}")
                break

            if _is_cloudflare_challenge(r):
                print(
                    "[BI21] Cloudflare challenge (cf-mitigated/Just a moment) — "
                    "site inaccessible en httpx. Voir blacklist."
                )
                break
            if r.status_code != 200:
                print(f"[BI21] HTTP {r.status_code} page {page}")
                break

            cards = _select_cards(BeautifulSoup(r.text, "html.parser"))
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                bien = _parse_card(card)
                if not bien:
                    continue

                cp = bien.get("code_postal") or ""
                dept = cp[:2] if len(cp) >= 2 else ""
                if departements and dept not in departements:
                    continue
                bien["departement"] = dept

                aid = bien.get("id_annonce") or bien.get("url")
                if aid in seen:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                seen.add(aid)
                results.append(bien)
                new_on_page += 1

            if new_on_page == 0:
                break
            await asyncio.sleep(0.5)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[BI21] Dept {dept}: {n} annonces")

    return results


def _select_cards(soup: BeautifulSoup) -> list:
    """Sélecteurs SSR best-effort — à confirmer une fois Cloudflare contourné."""
    for sel in (
        "div.annonce",
        "article.annonce",
        "div.bien",
        "div.product-item",
        "li.annonce",
        "div.listing-item",
    ):
        cards = soup.select(sel)
        if cards:
            return cards
    return []


def _parse_card(card) -> dict | None:
    try:
        a = card.select_one("a[href]")
        if not a:
            return None
        href = a.get("href", "")
        if not href:
            return None
        url = href if href.startswith("http") else BASE_URL + "/" + href.lstrip("/")

        text = card.get_text(" ", strip=True)

        if _EXCLUDE_TYPE.search(text) and not _KEEP_TYPE.search(text):
            return None

        type_bien = "maison"
        m_t = _KEEP_TYPE.search(text)
        if m_t:
            type_bien = m_t.group(0).lower()

        m_cp = re.search(r"\b(\d{5})\b", text)
        code_postal = m_cp.group(1) if m_cp else ""

        ville = ""
        m_v = re.search(r"([A-ZÉÈÀ][\w' -]+?)\s*\(?\b\d{5}\b", text)
        if m_v:
            ville = m_v.group(1).strip()

        prix = _parse_num(text, r"([\d \xa0]{4,})\s*€")
        surface = _parse_num(text, r"([\d \xa0]+)\s*m²")

        titre_el = card.select_one("h2, h3, .titre, .title")
        titre = titre_el.get_text(" ", strip=True) if titre_el else text[:120]

        return {
            "source": "bi21",
            "url": url,
            "id_annonce": url,
            "titre": titre[:150],
            "type_bien": type_bien,
            "description": None,
            "departement": code_postal[:2],
            "ville": ville[:80],
            "code_postal": code_postal,
            "surface": surface,
            "surface_terrain": None,
            "pieces": None,
            "chambres": None,
            "prix": prix,
            "dpe": None,
            "photos": [],
            "agence": "Bourse Immobilière 21",
        }
    except Exception:
        return None


def _parse_num(text: str, pattern: str) -> float | None:
    m = re.search(pattern, text)
    if not m:
        return None
    cleaned = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


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
    print(f"\nTotal BI21 (depts cibles): {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]} — {b['prix']}€"
            f" — {b.get('surface') or '?'}m² — {b['ville']} ({b['type_bien']})"
        )

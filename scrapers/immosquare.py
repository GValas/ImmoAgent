"""scrapers/immosquare.py — Immosquare (réseau d'agences Lyon / Grenoble / Ferney)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress + Divi + WP Grid Builder)
URL pattern : /a-vendre/   (page unique listant les biens, pagination AJAX wpgb
              NON exploitable en httpx → seules les ~20 premières cartes SSR
              sont disponibles).

Cartes : article.wpgb-card
  - URL   : a.wpgb-card-layer-link[href]  → /a-vendre-{tN}-{type}-{ville}-{id}-immosquare38.html
  - Titre : .titreCard       → "Vente maison" / "Vente appartement"
  - Ville : .villeCard       → "Grenoble" (PAS de code postal dans la carte)
  - Prix  : .prixCard        → "304 000€"
  - Pièces: .nbrPiecesTotalCard → "4 pièces"
  - Surf  : .surfaceCard     → "85 m2"
  - Photo : .wpgb-lazy-load[data-wpgb-src]

Stratégie filtre département :
  Les cartes n'exposent PAS de code postal, et le site n'a AUCUN paramètre URL
  de filtrage par département (la facette de géoloc passe par AJAX wpgb).
  → on récupère le code postal sur la PAGE DÉTAIL de chaque bien (le CP de la
    ville apparaît dans le HTML détail), puis post-filtre STRICT code_postal[:2].
  C'est volontairement prudent : 0 fuite hors-zone garantie.

Couverture réelle : Auvergne-Rhône-Alpes uniquement — Rhône (69 : Lyon, Bron,
  Villeurbanne, Vénissieux…), Isère (38 : Grenoble, Voiron, Crolles…) et Ain
  (01 : Ferney-Voltaire, Ornex). AUCUN bien dans la zone cible actuelle
  (72/28/45/89/49/37/36/18/58/41/53) → scraper conservé, actif: false.
  Réactiver si la zone de recherche s'étend à 38/69/01.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.immosquare.fr"
LIST_URL = f"{BASE_URL}/a-vendre/"
PHOTOS_PER_CARD = 5
DETAIL_CONCURRENCY = 4

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Codes postaux des départements couverts par le réseau (préfixes acceptés).
# Sert uniquement à borner la recherche du CP sur la page détail ; le filtre
# final reste code_postal[:2] == dept.
_COVERED = ("38", "69", "01")

_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|bien",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"loft|studio",
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
        try:
            r = await client.get(LIST_URL)
        except Exception as e:
            print(f"[Immosquare] Erreur récupération liste: {e}")
            return results
        if r.status_code != 200:
            print(f"[Immosquare] Liste status {r.status_code}")
            return results

        cards = BeautifulSoup(r.text, "html.parser").select("article.wpgb-card")
        print(f"[Immosquare] {len(cards)} cartes SSR sur la page liste")

        parsed: list[dict] = []
        seen_urls: set[str] = set()
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien or bien["url"] in seen_urls:
                continue
            seen_urls.add(bien["url"])
            parsed.append(bien)

        # Enrichissement CP via page détail (concurrence limitée), puis filtre dept.
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def enrich(bien: dict):
            async with sem:
                cp = await _fetch_cp(client, bien["url"], bien["ville"])
                await asyncio.sleep(0.3)
                return bien, cp

        enriched = await asyncio.gather(*(enrich(b) for b in parsed))

        for bien, cp in enriched:
            if not cp:
                # CP introuvable → on ne peut pas garantir le département → on écarte
                continue
            dept = cp[:2]
            if dept not in departements:
                continue
            bien["code_postal"] = cp
            bien["departement"] = dept

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            results.append(bien)

    for dept in departements:
        n = sum(1 for b in results if b["departement"] == dept)
        if n:
            print(f"[Immosquare] Dept {dept}: {n} annonces")

    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a.wpgb-card-layer-link[href]") or card.select_one(
        "a.prixCard[href]"
    )
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    titre_el = card.select_one(".titreCard")
    titre_raw = titre_el.get_text(" ", strip=True) if titre_el else ""

    # Type de bien depuis le titre ("Vente maison") ou l'URL
    type_src = (titre_raw + " " + url).lower()
    if _EXCLUDE_TYPE.search(type_src) and not _KEEP_TYPE.search(titre_raw.lower()):
        return None
    if not _KEEP_TYPE.search(type_src):
        return None
    m_type = re.search(
        r"(maison|appartement|villa|propri[eé]t[eé]|ferme|domaine|ch[aâ]teau|"
        r"manoir|moulin|demeure|mas|gite|gîte)",
        type_src,
    )
    type_bien = m_type.group(1) if m_type else "bien"
    # on a déjà exclu les appartements via _EXCLUDE_TYPE plus haut
    if type_bien == "appartement":
        return None

    ville_el = card.select_one(".villeCard")
    ville = ville_el.get_text(" ", strip=True) if ville_el else ""

    prix_el = card.select_one(".prixCard")
    prix = _parse_price(prix_el.get_text(" ", strip=True) if prix_el else "")

    pieces = _parse_int(
        r"(\d+)\s*pi[eè]ces?",
        (card.select_one(".nbrPiecesTotalCard") or _Empty()).get_text(" ", strip=True),
    )
    surface = _parse_surface(
        (card.select_one(".surfaceCard") or _Empty()).get_text(" ", strip=True)
    )

    # id_annonce depuis le slug : ...-{id}-immosquare38.html
    m_id = re.search(r"-(\d+)-immosquare", url)
    id_annonce = m_id.group(1) if m_id else url

    photos = []
    for img in card.select(".wpgb-lazy-load"):
        src = img.get("data-wpgb-src") or ""
        if src and not src.startswith("data:"):
            photos.append(src)
    if not photos:
        for img in card.select("img.wpgb-noscript-img"):
            src = img.get("src") or ""
            if src and not src.startswith("data:"):
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    titre = titre_raw
    if ville:
        titre = f"{titre_raw} {ville}".strip()

    return {
        "source": "immosquare",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": "",      # rempli après enrichissement CP
        "ville": ville[:80],
        "code_postal": "",      # rempli après enrichissement CP
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Immosquare",
    }


async def _fetch_cp(
    client: httpx.AsyncClient, url: str, ville: str
) -> str | None:
    """Récupère le code postal du bien sur sa page détail.

    Le CP de la ville du bien apparaît dans le HTML détail (zone adresse/carte).
    La page contient aussi des CP d'annonces liées : on privilégie un CP situé à
    proximité immédiate du nom de la ville, sinon le 1er CP d'un dept couvert.
    """
    try:
        r = await client.get(url)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    t = r.text

    # 1) CP proche du nom de la ville (fenêtre de texte)
    if ville:
        for m in re.finditer(re.escape(ville), t):
            seg = t[max(0, m.start() - 60) : m.start() + 60]
            cps = re.findall(r"\b(\d{5})\b", seg)
            for cp in cps:
                if cp[:2] in _COVERED:
                    return cp

    # 2) Repli : 1er CP appartenant à un département couvert
    for cp in re.findall(r"\b(\d{5})\b", t):
        if cp[:2] in _COVERED:
            return cp
    return None


# ── Helpers ──────────────────────────────────────────────────────────────────

class _Empty:
    def get_text(self, *a, **k):
        return ""


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_surface(text: str) -> float | None:
    m = re.search(r"([\d\s\xa0]+)\s*m", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 5 <= f <= 5000:
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
    print(f"\nTotal Immosquare: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

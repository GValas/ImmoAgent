"""scrapers/danielfeau.py — Daniel Féau / Belles Demeures de France (immobilier de prestige)

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /fr/listing/france/vente/maisons?page={N}
              → listing NATIONAL (châteaux, manoirs, propriétés, maisons de prestige).
              AUCUN slug ni paramètre département : le site n'expose ni code postal
              ni coordonnées dans le HTML (ni liste ni détail). On récupère donc tout
              le national puis on filtre en POST-FILTRE par département.

Filtre département : les cartes ne donnent QUE le nom de ville (ex. "Salbris",
              "Le Lude", "Romorantin-Lanthenay"). On résout ville → code INSEE/CP via
              l'API publique geo gratuite (api-adresse.data.gouv.fr, type=municipality).
              Le département = 2 premiers chiffres du code INSEE (citycode), source
              autoritaire → 0 fuite hors-zone. Géocodage caché par nom de ville
              (chaque ville résolue une seule fois par run).

Cartes : li.property.initial[data-property-id]
  - id    : @data-property-id (réf numérique)
  - URL   : a[href] → /fr/annonce-immobiliere/{id}
  - H2    : "Achat {type}, {Ville}, {N} pièces, {surf} m², ref {id}"  +  span.price
  - H3    : <span>Type</span><span>{surf} m² <em>({terrain} m²)</em></span><span>{N} pièces</span>
  - Texte : p.comment (réf + début de description)
  - Photo : div > a > img[src]

Type de bien : 1er segment du H2 ("château", "manoir", "propriété", "maison"...).
               On exclut les appartements.

Couverture : réseau de prestige national (~490 biens), implantation forte Île-de-France /
             Sud, mais présence réelle en zone Val-de-Loire (Sologne 41, Sarthe 72,
             Eure-et-Loir 28...). Stock par département cible faible mais réel.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://danielfeau.com"
LISTING_PATH = "/fr/listing/france/vente/maisons"
GEO_URL = "https://api-adresse.data.gouv.fr/search/"
MAX_PAGES = 50
PHOTOS_PER_CARD = 10
GEO_CONCURRENCY = 6

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien (1er mot du H2) à exclure
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"hôtel particulier|loft|duplex|studio",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)
    dept_set = set(departements)

    raw_cards: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1) Récupère tout le national (le site n'a pas de filtre dept serveur)
        for page in range(1, MAX_PAGES + 1):
            try:
                cards = await _scrape_page(client, page)
            except Exception as e:
                print(f"[DanielFeau] Erreur page {page}: {e}")
                break
            if not cards:
                break
            new = 0
            for c in cards:
                if c["id_annonce"] in seen_ids:
                    continue
                seen_ids.add(c["id_annonce"])
                raw_cards.append(c)
                new += 1
            if new == 0:
                break
            await asyncio.sleep(0.5)

        print(f"[DanielFeau] {len(raw_cards)} biens nationaux récupérés")

        # 2) Résout ville → (code_postal, dept) via l'API geo, cache par ville
        villes = sorted({c["ville"] for c in raw_cards if c["ville"]})
        geo_cache = await _geocode_villes(client, villes)

    # 3) Post-filtre STRICT par département (source : code INSEE de la ville)
    results: list[dict] = []
    for c in raw_cards:
        cp, dept = geo_cache.get(c["ville"], (None, None))
        if not dept or dept not in dept_set:
            continue

        # Sécurité supplémentaire : cohérence CP / dept
        if cp and cp[:2] != dept:
            continue

        c["code_postal"] = cp or ""
        c["departement"] = dept

        p = c.get("prix") or 0
        s = c.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        results.append(c)

    # Comptage par dept (log)
    par_dept: dict[str, int] = {}
    for b in results:
        par_dept[b["departement"]] = par_dept.get(b["departement"], 0) + 1
    for d in sorted(par_dept):
        print(f"[DanielFeau] Dept {d}: {par_dept[d]} annonces")

    return results


async def _scrape_page(client: httpx.AsyncClient, page: int) -> list[dict]:
    url = f"{BASE_URL}{LISTING_PATH}"
    r = await client.get(url, params={"page": page})
    if r.status_code != 200:
        return []
    cards = BeautifulSoup(r.text, "html.parser").select("li.property.initial")
    out: list[dict] = []
    for card in cards:
        try:
            bien = _parse_card(card)
        except Exception:
            continue
        if bien:
            out.append(bien)
    return out


def _parse_card(card) -> dict | None:
    aid = card.get("data-property-id") or card.get("id") or ""
    aid = str(aid).strip()
    if not aid:
        return None

    link = card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    url = href if href.startswith("http") else BASE_URL + href
    if not href:
        url = f"{BASE_URL}/fr/annonce-immobiliere/{aid}"

    # H2 : "Achat {type}, {Ville}, {N} pièces, {surf} m², ref {id}"
    h2 = card.select_one("h2 > div")
    h2_txt = h2.get_text(" ", strip=True) if h2 else ""
    type_bien, ville = _parse_h2(h2_txt)
    if not ville:
        return None
    if _EXCLUDE_TYPE.search(type_bien):
        return None

    # Prix
    price_el = card.select_one("h2 .price, .price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # H3 : surface habitable / terrain / pièces
    h3 = card.select_one("h3")
    h3_txt = h3.get_text(" ", strip=True) if h3 else ""
    surface = _parse_surface(h3_txt)
    surface_terrain = _parse_terrain(h3_txt)
    pieces = _parse_pieces(h3_txt) or _parse_pieces(h2_txt)

    # Description / référence
    comment = card.select_one("p.comment")
    description = comment.get_text(" ", strip=True) if comment else ""
    ref_el = card.select_one(".reference")
    ref = ""
    if ref_el:
        m = re.search(r"(\d+)", ref_el.get_text())
        if m:
            ref = m.group(1)
    id_annonce = ref or aid

    # Photos (lazy / src)
    photos: list[str] = []
    for img in card.select("img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    # dédup en gardant l'ordre
    seen = set()
    photos = [p for p in photos if not (p in seen or seen.add(p))][:PHOTOS_PER_CARD]

    titre = h2_txt or f"{type_bien.title()} {ville}".strip()

    return {
        "source": "danielfeau",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien or "maison",
        "description": description[:1200],
        "departement": "",          # rempli après géocodage
        "ville": ville[:80],
        "code_postal": "",          # rempli après géocodage
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Daniel Féau / Belles Demeures de France",
    }


# ── Géocodage ville → (code_postal, dept) ─────────────────────────────────────

async def _geocode_villes(
    client: httpx.AsyncClient, villes: list[str]
) -> dict[str, tuple[str | None, str | None]]:
    """Résout chaque nom de ville en (code_postal, departement) via geo.data.gouv.fr.
    Le département vient des 2 premiers chiffres du code INSEE (citycode), autoritaire.
    """
    sem = asyncio.Semaphore(GEO_CONCURRENCY)
    cache: dict[str, tuple[str | None, str | None]] = {}

    async def one(ville: str):
        async with sem:
            try:
                r = await client.get(
                    GEO_URL,
                    params={"q": ville, "type": "municipality", "limit": 1},
                    timeout=15,
                )
                feats = r.json().get("features", [])
                if feats:
                    pr = feats[0]["properties"]
                    citycode = pr.get("citycode") or ""
                    cp = pr.get("postcode")
                    # Corse : citycode commence par 2A/2B
                    dept = citycode[:2] if citycode else (cp[:2] if cp else None)
                    cache[ville] = (cp, dept)
                    return
            except Exception:
                pass
            cache[ville] = (None, None)

    await asyncio.gather(*(one(v) for v in villes))
    return cache


# ── Helpers de parsing ────────────────────────────────────────────────────────

def _parse_h2(text: str) -> tuple[str, str]:
    """'Achat château, Salbris, 25 pièces, 750 m², ref 87087719'
       → ('château', 'Salbris')
       Gère aussi 'Paris 7ème (75007)' → ('appartement', 'Paris 7ème').
    """
    if not text:
        return "", ""
    # retire le préfixe "Achat "
    body = re.sub(r"^\s*Achat\s+", "", text, flags=re.IGNORECASE)
    parts = [p.strip() for p in body.split(",")]
    if len(parts) < 2:
        return (parts[0].lower() if parts else ""), ""
    type_bien = parts[0].lower()
    ville = parts[1]
    # retire un éventuel (75007) accolé
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", ville).strip()
    return type_bien, ville


def _parse_price(text: str) -> float | None:
    if not text or "demande" in text.lower():
        return None
    cleaned = re.sub(r"[^\d]", "", text.replace("\xa0", "").replace(" ", ""))
    try:
        v = float(cleaned) if cleaned else None
        return v if v and v > 1000 else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'Château 750 m² (830 m²) 25 pièces' → 750 (1ère surface = habitable)."""
    m = re.search(r"([\d\s\xa0 ]+)\s*m", text)
    if m:
        val = re.sub(r"[\s\xa0 ]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 20000:
                return f
        except ValueError:
            pass
    return None


def _parse_terrain(text: str) -> float | None:
    """La 2e surface entre parenthèses '(830 m²)' = terrain/surface totale."""
    m = re.search(r"\(\s*([\d\s\xa0 ]+)\s*m", text)
    if m:
        val = re.sub(r"[\s\xa0 ]", "", m.group(1))
        try:
            f = float(val)
            if f > 0:
                return f
        except ValueError:
            pass
    return None


def _parse_pieces(text: str) -> int | None:
    m = re.search(r"(\d+)\s*pi[eè]ces?", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


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
    print(f"\nTotal Daniel Féau: {len(biens)} annonces")
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

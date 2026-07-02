"""scrapers/chateauxpourtous_rustique.py — Chateauxpourtous-rustique (portail niche)

Portail spécialisé maisons en pierre ancienne / rustiques / à restaurer et
demeures de caractère, multi-régions (France entière).

Méthode : scrape_simple (httpx) — SSR HTML pur (pages .php, aucun JS requis).

URL pattern :
  - Pages RÉGION : /{region}-{cat}-...-{liste-depts}.php
      ex : /centre-1-...-18-28-36-37-41-45.php
    Chaque région a 4 catégories de biens :
      1 = maison en pierre ancienne / rustique / à restaurer
      2 = belle demeure ancienne de caractère / de charme
      3 = manoir / maison de maître / presbytère / chapelle / prieuré
      4 = château / haras / domaine / demeure de prestige
    La page liste TOUS les départements de la région mélangés
    → PAS de filtre département côté serveur → POST-FILTRE STRICT sur code_postal[:2].
  - Pages DÉTAIL : /...-{ville}-{CP}.php  (le code postal est dans l'URL détail,
    présent aussi sur la carte de liste → c'est notre source de CP fiable).

Cartes : div.absolubiens (certains blocs n'ont pas de bien → on exige a.bulle)
  - Date     : .souligne                     → "05/10/25 :"
  - Type     : nœud texte après .souligne     → "Vente Prieuré" → "Prieuré"
  - Dept (nom): .grand b                       → "INDRE ET LOIRE"
  - Prix+desc: .centre h3                       → "185 000 € (HAI) ...<strong>desc</strong>"
  - URL/CP   : a.bulle[href]                     → CP = dernier \\d{5} avant .php
  - Photos   : a.bulle img[src] (relatifs → BASE_URL)
  - Desc long: h4

Stratégie filtre département : on mappe chaque département cible vers sa région,
on balaie les 4 catégories de la région, puis POST-FILTRE code_postal[:2] == dept
(0 fuite garanti, les pages région mélangent les départements).

Couverture : inventaire de niche, faible mais réel ; certains départements cibles
ont 0 bien à un instant T (stock qui tourne).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.chateauxpourtous-rustique.fr"
PHOTOS_PER_CARD = 8


# Département cible → région (slug du portail). Le portail organise les annonces
# par grande région ; un département est atteint via la page région + post-filtre CP.
DEPT_REGION: dict[str, str] = {
    # Auvergne (segment d'origine du site)
    "03": "auvergne", "15": "auvergne", "43": "auvergne", "63": "auvergne",
    # Centre - Val de Loire
    "18": "centre", "28": "centre", "36": "centre",
    "37": "centre", "41": "centre", "45": "centre",
    # Bourgogne
    "21": "bourgogne", "58": "bourgogne", "71": "bourgogne", "89": "bourgogne",
    # Pays de la Loire (note : 43 est listé ici aussi sur le site, mais 43=Haute-Loire → auvergne)
    "44": "pays-de-la-loire", "49": "pays-de-la-loire", "53": "pays-de-la-loire",
    "72": "pays-de-la-loire", "85": "pays-de-la-loire",
    # Quelques autres régions courantes (pour réutilisation hors zone test)
    "19": "limousin", "23": "limousin", "87": "limousin",
    "16": "poitou-charente", "17": "poitou-charente", "79": "poitou-charente", "86": "poitou-charente",
}

# URL de catégorie 1 d'une région (point d'entrée ; les cat 2/3/4 sont découvertes
# dynamiquement depuis cette page pour ne pas figer les longs slugs).
REGION_CAT1: dict[str, str] = {
    "centre": "centre-1-achat-vente-maison-en-pierre-ancienne-pas-chere-a-restaurer-a-vendre-centre-val-de-loire-cher-eure-et-loir-indre-indre-et-loire-loir-et-cher-loiret-orleans-18-28-36-37-41-45.php",
    "bourgogne": "bourgogne-1-achat-vente-maison-en-pierre-ancienne-pas-chere-a-restaurer-a-vendre-bourgogne-cote-d-or-nievre-saone-et-loire-yonne-dijon-21-58-71-89.php",
    "pays-de-la-loire": "pays-de-la-loire-1-achat-vente-maison-en-pierre-ancienne-pas-chere-a-restaurer-a-vendre-pays-de-la-loire-atlantique-maine-et-loire-mayenne-sarthe-vendee-nantes-43-49-53-72-85.php",
    "auvergne": "auvergne-1-achat-vente-maison-en-pierre-ancienne-pas-chere-a-restaurer-a-vendre-auvergne-allier-cantal-haute-loire-puy-de-dome-clermont-ferrand-03-15-43-63.php",
    "limousin": "limousin-1-achat-vente-maison-en-pierre-ancienne-pas-chere-a-restaurer-a-vendre-limousin-correze-creuse-haute-vienne-limoges-19-23-87.php",
    "poitou-charente": "poitou-charente-1-achat-vente-maison-en-pierre-ancienne-pas-chere-a-restaurer-a-vendre-poitou-charentes-charente-maritime-deux-sevres-vienne-16-17-79-86.php",
}

_CP_RE = re.compile(r"-(\d{5})\.php")


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Regrouper les départements par région pour ne télécharger chaque région qu'une fois.
    region_to_depts: dict[str, set[str]] = {}
    for dept in departements:
        region = DEPT_REGION.get(dept)
        if region and region in REGION_CAT1:
            region_to_depts.setdefault(region, set()).add(dept)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for region, depts in region_to_depts.items():
            try:
                biens = await _scrape_region(
                    client, region, depts, prix_max, prix_min, surface_min, seen_ids
                )
                results.extend(biens)
                tally: dict[str, int] = {}
                for b in biens:
                    d = b["code_postal"][:2] if b["code_postal"] else "??"
                    tally[d] = tally.get(d, 0) + 1
                print(f"[ChateauxRustique] Région {region}: {len(biens)} annonces {tally}")
            except Exception as e:
                print(f"[ChateauxRustique] Erreur région {region}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_region(
    client: httpx.AsyncClient,
    region: str,
    depts: set[str],
    prix_max: int,
    prix_min: int,
    surface_min: int,
    seen_ids: set[str],
) -> list[dict]:
    biens: list[dict] = []

    cat1 = REGION_CAT1[region]
    r1 = await client.get(f"{BASE_URL}/{cat1}")
    if r1.status_code != 200:
        return biens

    # Découvrir les URLs des catégories 2/3/4 depuis la page cat 1.
    cat_links = sorted(set(re.findall(
        rf'href=["\']({re.escape(region)}-[234]-[^"\']+\.php)', r1.text
    )))
    urls = [cat1] + cat_links

    for i, u in enumerate(urls):
        html = r1.text if u == cat1 else None
        if html is None:
            r = await client.get(f"{BASE_URL}/{u}")
            if r.status_code != 200:
                continue
            html = r.text
            await asyncio.sleep(0.5)

        for card in BeautifulSoup(html, "html.parser").select("div.absolubiens"):
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue

            cp = bien["code_postal"]
            # POST-FILTRE DÉPARTEMENT STRICT — 0 fuite.
            if not cp or cp[:2] not in depts:
                continue
            bien["departement"] = cp[:2]

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


def _parse_card(card) -> dict | None:
    link = card.select_one("a.bulle")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + "/" + href.lstrip("/")

    m_cp = _CP_RE.search(href)
    if not m_cp:
        return None
    code_postal = m_cp.group(1)

    # id_annonce : segment id du slug détail (ex : demirand-37_23598-...) ; secours = url
    id_annonce = url
    m_id = re.search(r"/([a-z0-9]+-[A-Za-z0-9_]+)-acheter", href, re.IGNORECASE)
    if m_id:
        id_annonce = m_id.group(1)
    else:
        m_id2 = re.search(r"/[^/]*?(\d{4,})[^/]*\.php", href)
        if m_id2:
            id_annonce = m_id2.group(1)

    # Type de bien : nœud texte juste après .souligne ("Vente Prieuré" → "Prieuré")
    type_bien = "maison"
    soul = card.select_one(".souligne")
    if soul and soul.next_sibling:
        raw_type = str(soul.next_sibling).strip()
        raw_type = re.sub(r"^Vente\s+", "", raw_type, flags=re.IGNORECASE).strip()
        if raw_type:
            type_bien = raw_type

    # Prix + description courte (h3 contient prix puis <strong>)
    h3 = card.select_one(".centre h3")
    h3_text = h3.get_text(" ", strip=True) if h3 else ""
    prix = _parse_price(h3_text)
    strong = h3.select_one("strong") if h3 else None
    desc_courte = strong.get_text(" ", strip=True) if strong else ""

    # Description longue
    h4 = card.select_one("h4")
    desc_longue = h4.get_text(" ", strip=True) if h4 else ""
    description = (desc_courte + " " + desc_longue).strip()

    # Ville depuis le slug détail (segment avant le CP, capitalisé sur le site)
    ville = _ville_from_href(href, code_postal)
    if not ville and desc_courte:
        # secours : 1er token avant virgule de la description
        ville = desc_courte.split(",")[0].strip()[:80]

    titre = desc_courte[:150] or f"{type_bien} {ville}".strip()

    surface = _parse_surface_hab(description)
    surface_terrain = _parse_terrain(description)

    # Photos (relatives → absolues)
    photos = []
    for img in link.select("img"):
        src = img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif not src.startswith("http"):
                src = BASE_URL + "/" + src.lstrip("/")
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "chateauxpourtous_rustique",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre,
        "type_bien": type_bien.lower(),
        "description": description[:1200],
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Chateauxpourtous-rustique",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ville_from_href(href: str, cp: str) -> str:
    """Le slug détail finit par '-{Ville}-{CP}.php' (Ville en CamelCase/tirets).

    Ex : '...-sarthe-Saint-Vincent-Du-Lorouer-72150.php' → 'Saint Vincent Du Lorouer'.
    On capture la séquence de tokens commençant par une majuscule juste avant le CP
    (les segments génériques précédents sont en minuscules).
    """
    m = re.search(
        r"-([A-Z][A-Za-z']*(?:-[A-Za-z][A-Za-z']*)*)-" + re.escape(cp) + r"\.php",
        href,
    )
    if m:
        return m.group(1).replace("-", " ").strip()
    return ""


def _parse_price(text: str) -> float | None:
    m = re.search(r"([\d][\d\s\xa0]*)\s*€", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
        return f if f > 1000 else None
    except ValueError:
        return None


def _parse_surface_hab(text: str) -> float | None:
    """Cherche 'NNN m2 habitable(s)' dans le texte libre."""
    if not text:
        return None
    m = re.search(
        r"(\d[\d\s\xa0]*)\s*m[²2]\s*(?:hab|habitable|de surface)",
        text, re.IGNORECASE,
    )
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 3000:
                return f
        except ValueError:
            pass
    return None


def _parse_terrain(text: str) -> float | None:
    """Cherche 'NNNN m2 de jardin/terrain/parc' dans le texte libre."""
    if not text:
        return None
    m = re.search(
        r"(\d[\d\s\xa0]*)\s*m[²2]\s*(?:de\s+)?(?:jardin|terrain|parc|terre)",
        text, re.IGNORECASE,
    )
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if f >= 50:
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
    print(f"\nTotal Chateauxpourtous-rustique : {len(biens)} annonces")
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

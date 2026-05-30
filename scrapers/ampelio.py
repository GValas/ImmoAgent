"""scrapers/ampelio.py — Ampelio (courtage de DOMAINES VITICOLES, Loire & Auvergne)

Méthode : scrape_simple (httpx) — SSR WordPress.

⚠️ HORS PÉRIMÈTRE / INACTIF (actif: false dans sources.yaml).
Ampelio n'est PAS un portail de biens résidentiels : c'est un courtier spécialisé
dans la cession de *domaines viticoles* (exploitations vendues à l'hectare, 6–42 ha).
Aucun des 17 biens du site n'est une maison / manoir / longère au sens du projet
(ce sont des entreprises agricoles, certaines « avec maison d'habitation »).

Pourquoi le filtre département est impossible (→ blacklist) :
  - Les annonces n'exposent NI code postal NI commune (ni en liste ni en page détail) :
    la localisation est volontairement floutée à l'échelle de l'APPELLATION
    (vente confidentielle). Le seul CP présent (49170) est l'adresse de l'agence.
  - On ne dispose que d'un slug/région d'appellation (`Saumur`, `Touraine`,
    `Bourgueil · Chinon · St Nicolas`, `Anjou`, `Muscadet · Pays Nantais`,
    `Auvergne`…). Une appellation chevauche plusieurs départements
    (Touraine → 37/41/36, Saumur → 49/86, Anjou → 49…) sans CP pour trancher,
    donc impossible de garantir 0 fuite.
  - Prix : seulement une *tranche* (`data-price="800000-1500000"`), pas un montant.
  - Surface : en hectares (vignoble), pas de m² habitable ni de pièces.

Listing : https://ampelio.fr/proprietes-a-la-vente/  (~17 cartes, tout sur 1 page)
          slugs appellation : /proprietes-a-la-vente/{appellation-slug}/
Cards : a.card-catalog-item
  - href            : URL détail
  - title           : titre
  - data-price      : tranche "min-max" (€) — pas un prix exact
  - .tax-region     : libellé appellation ("Saumur", "Bourgueil · Chinon · St Nicolas")
  - .id-vignoble    : "REF: 332"
  - img.img-project : photo de couverture

Le code ci-dessous reste fonctionnel (parse + mappe appellation→départements et
post-filtre sur cette base) mais ne doit être réactivé que si Ampelio se met à
exposer une localisation fine (CP/commune) — sinon il fuit par construction.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://ampelio.fr"
LISTING_URL = f"{BASE_URL}/proprietes-a-la-vente/"
PHOTOS_PER_CARD = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Appellation (libellé .tax-region OU slug) → départements potentiels.
# ⚠️ Mapping APPROXIMATIF : une appellation couvre plusieurs départements et il
# n'y a aucun CP pour lever l'ambiguïté → ne garantit PAS l'absence de fuite.
APPELLATION_DEPTS: dict[str, list[str]] = {
    "saumur": ["49", "86"],
    "touraine": ["37", "41", "36"],
    "sancerre-pouilly-sur-loire": ["18", "58"],
    "montlouis-sur-loire-vouvray": ["37"],
    "bourgueil-chinon-st-nicolas": ["37"],
    "menetou-salon-quincy-reuilly": ["18", "36"],
    "anjou": ["49"],
    "muscadet-pays-nantais": ["44"],
    "auvergne": ["63", "03", "15", "43"],
}


def _appellation_key(card_href: str, region_label: str) -> str:
    """Déduit la clé d'appellation depuis le slug d'URL (fiable) ou le libellé."""
    m = re.search(r"/proprietes-a-la-vente/([^/]+)/", card_href or "")
    if m:
        return m.group(1).lower()
    # secours : libellé région normalisé
    lbl = (region_label or "").lower()
    lbl = lbl.replace(" · ", "-").replace("·", "-").replace(" ", "-")
    lbl = re.sub(r"[^a-z0-9-]", "", lbl)
    return lbl


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]

    results: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        try:
            r = await client.get(LISTING_URL)
        except Exception as e:
            print(f"[Ampelio] Erreur listing: {e}")
            return results
        if r.status_code != 200:
            print(f"[Ampelio] Listing status {r.status_code}")
            return results

        cards = [
            c for c in BeautifulSoup(r.text, "html.parser").select("a.card-catalog-item")
            if c.get("href")
        ]

        for card in cards:
            bien = _parse_card(card)
            if not bien:
                continue

            # Post-filtre département via mapping appellation (best effort, non fiable).
            depts = bien.pop("_depts_possibles", [])
            if departements:
                inter = [d for d in depts if d in departements]
                if not inter:
                    continue
                # On ne peut pas trancher → on retient le 1er dept cible plausible.
                bien["departement"] = inter[0]

            aid = bien.get("id_annonce") or bien.get("url")
            if aid in seen:
                continue
            seen.add(aid)
            results.append(bien)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b.get("departement") or "??"] = by_dept.get(b.get("departement") or "??", 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[Ampelio] Dept {dept}: {n} annonces (localisation approx. appellation)")

    return results


def _parse_card(card) -> dict | None:
    href = card.get("href", "").strip()
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    titre = (card.get("title") or "").strip()
    region_el = card.select_one(".tax-region")
    region_label = region_el.get_text(" ", strip=True) if region_el else ""
    if not titre:
        h3 = card.select_one("h3")
        titre = h3.get_text(" ", strip=True) if h3 else region_label

    # Référence
    ref_el = card.select_one(".id-vignoble")
    ref = ""
    if ref_el:
        m = re.search(r"(\d+)", ref_el.get_text())
        if m:
            ref = m.group(1)
    id_annonce = ref or url

    # Tranche de prix (data-price="min-max") → on garde le bas de fourchette (indicatif)
    prix = None
    dp = card.get("data-price") or ""
    m_dp = re.match(r"(\d+)\s*-\s*(\d+)", dp)
    if m_dp:
        lo, hi = int(m_dp.group(1)), int(m_dp.group(2))
        prix = lo or hi or None

    # Surface en hectares depuis le titre → m² (terrain viticole)
    surface_terrain = None
    m_ha = re.search(r"(\d+(?:[.,]\d+)?)\s*hectare", titre, re.IGNORECASE)
    if m_ha:
        try:
            surface_terrain = float(m_ha.group(1).replace(",", ".")) * 10000
        except ValueError:
            pass

    # Photo
    photos = []
    img = card.select_one("img.img-project") or card.select_one("img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src.startswith("http"):
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    appel = _appellation_key(href, region_label)
    depts_possibles = APPELLATION_DEPTS.get(appel, [])

    return {
        "source": "ampelio",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "domaine viticole",
        "description": region_label,
        "departement": depts_possibles[0] if depts_possibles else "",
        "ville": "",                 # non exposé (localisation floutée à l'appellation)
        "code_postal": "",           # non exposé
        "surface": None,             # pas de m² habitable (vignoble)
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": None,
        "prix": prix,                # ⚠️ bas de fourchette, pas un prix exact
        "dpe": None,
        "photos": photos,
        "agence": "Ampelio",
        "_depts_possibles": depts_possibles,
    }


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
    print(f"\nTotal Ampelio (depts cibles): {len(biens)} annonces")
    depts = sorted({b.get("departement") for b in biens if b.get("departement")})
    print(f"Départements retenus (approx. appellation) : {depts}")
    for b in biens[:15]:
        print(
            f"  [{b.get('departement') or '??'}] {b['titre'][:55]}"
            f" — {b['prix']}€(tranche)"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['description']}"
        )

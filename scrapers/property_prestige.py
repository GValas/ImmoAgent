"""scrapers/property_prestige.py — Property Prestige (domaines & propriétés de prestige)

Site : https://www.property-prestige.com — agrégateur WordPress (plugin
       "AllYouCanPost") de propriétés/domaines ruraux haut de gamme : exploitations
       agricoles avec hectares, châteaux, domaines viticoles, gîtes/réceptions…
       Concentration Sud/Occitanie (Gers, Tarn, Périgord, Var, Ardèche…).

Méthode : scrape_simple (httpx) — SSR HTML pur (pas de Playwright).

Énumération des fiches : sitemap WordPress
  /wp-sitemap.xml → /aycp_ad-sitemap1.xml  (toutes les annonces, ~98 URLs
  /collection/{theme}/{slug}/). Plus fiable que paginer chaque collection.

Localisation : le site n'expose **AUCUN code postal ni ville structurée**
  (JSON-LD PostalAddress vide, pas de data-* géo). La localisation n'apparaît
  qu'en TEXTE LIBRE : le titre nomme presque toujours le département / la grande
  région ("INDRE ET LOIRE DOMAINE…", "GERS DOMAINE…", "PERIGORD…"), et la fiche
  détail contient une ligne "Localisation Privilégiée : En <Département>".

Filtre département (STRICT, 0 fuite) : on mappe les 11 départements cibles à des
  motifs de NOM de département (avec frontières de mots, et noms COMPOSÉS testés
  AVANT les noms courts ambigus — « Indre-et-Loire » avant « Indre », « Cher »).
  On exige une correspondance dans le TITRE (signal fiable, le titre nomme le
  bien par sa localité) OU dans la ligne "Localisation Privilégiée" de la fiche.
  code_postal reste vide (non disponible) ; on remplit `departement`.

Carte liste (div.listing-card — sur /collection/all/ ou une collection) :
  h2 > a (titre + url), .price "Prix: 3 100 000 €", .main-data
  ("2000 M² Surface (construite)", "60 Ha Surface Terres"), background-image (photo).

Comme la carte porte déjà surface/terrain/prix/description, on n'ouvre la fiche
détail que pour confirmer/affiner la localisation (concurrence bornée, sleep 0.3).

Profil prestige rural Sud → 0 stock attendu dans la zone cible (72/28/45/89/49/
37/36/18/58/41/53), mais scraper fonctionnel + 0 fuite vérifié.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import unicodedata

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.property-prestige.com"
SITEMAP_INDEX = f"{BASE_URL}/wp-sitemap.xml"
PHOTOS_PER_CARD = 10
DETAIL_CONCURRENCY = 4

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Départements cibles → motifs de NOM (frontières de mots). Les noms COMPOSÉS
# sont placés AVANT les noms courts ambigus (Indre, Cher) : la recherche
# s'arrête au 1er match, donc « indre et loire » est capté par 37 avant que
# 36 (« indre ») ne s'y trompe. \b évite « vaCHER », « venDRE », « moINDRE ».
_DEPT_NAME_PATTERNS: list[tuple[str, str]] = [
    ("37", r"indre[\s\-]et[\s\-]loire"),
    ("28", r"eure[\s\-]et[\s\-]loir"),
    ("41", r"loir[\s\-]et[\s\-]cher"),
    ("49", r"maine[\s\-]et[\s\-]loire"),
    ("72", r"\bsarthe\b"),
    ("45", r"\bloiret\b"),
    ("89", r"\byonne\b"),
    ("36", r"\bindre\b"),
    ("18", r"\bcher\b"),
    ("58", r"\bni[èe]vre\b"),
    ("53", r"\bmayenne\b"),
]

# Types à exclure (le site est rural/prestige mais peut lister du commercial pur).
_EXCLUDE_TYPE = re.compile(
    r"\bhotel\b|\bbureau\b|fonds de commerce|local commercial", re.IGNORECASE
)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower()


def _match_dept(text: str, departements: set[str]) -> str | None:
    """Retourne le code département cible si `text` nomme l'un d'eux, sinon None.
    Frontières de mots + noms composés prioritaires → 0 faux positif."""
    t = _norm(text)
    for dept, pat in _DEPT_NAME_PATTERNS:
        if dept in departements and re.search(pat, t):
            return dept
    return None


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        ad_urls = await _list_ad_urls(client)
        print(f"[PropertyPrestige] {len(ad_urls)} fiches dans le sitemap")

        # Carte liste : on charge /collection/all/ pour récupérer prix/surface/
        # terrain/photo/description sans une requête par bien.
        cards = await _load_cards(client)
        print(f"[PropertyPrestige] {len(cards)} cartes liste chargées")

        # 1. Pré-filtre par le TITRE (signal localisation fiable) sur l'union
        #    sitemap ∪ cartes.
        candidats: dict[str, dict] = {}
        for url, card in cards.items():
            dept = _match_dept(card.get("titre", ""), departements)
            if dept:
                card["departement"] = dept
                candidats[url] = card
        # fiches du sitemap absentes des cartes : pré-filtre par le slug d'URL
        for url in ad_urls:
            if url in candidats or url in cards:
                continue
            title_guess = url.rstrip("/").split("/")[-1].replace("-", " ")
            dept = _match_dept(title_guess, departements)
            if dept:
                candidats[url] = {"url": url, "departement": dept, "titre": title_guess}

        print(f"[PropertyPrestige] {len(candidats)} candidats après pré-filtre titre")

        # 2. Confirmation détail (localisation + complétion des champs manquants).
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def confirm(url: str, b: dict):
            async with sem:
                try:
                    keep = await _confirm_detail(client, url, b, departements)
                except Exception as e:
                    print(f"[PropertyPrestige] Erreur détail {url}: {e}")
                    keep = True  # on a déjà un match titre → on conserve
                await asyncio.sleep(0.3)
                return keep

        keeps = await asyncio.gather(
            *(confirm(u, b) for u, b in candidats.items())
        )

    results: list[dict] = []
    for (url, b), keep in zip(candidats.items(), keeps):
        if not keep:
            continue
        bien = _finalize(url, b)
        # garde-fou département strict
        if bien["departement"] not in departements:
            continue
        t = (bien.get("type_bien") or "") + " " + (bien.get("titre") or "")
        if _EXCLUDE_TYPE.search(t):
            continue
        p = bien.get("prix") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        s = bien.get("surface") or 0
        if surface_min and s and s < surface_min:
            continue
        results.append(bien)

    print(f"[PropertyPrestige] {len(results)} biens retenus dans la zone cible")
    return results


async def _list_ad_urls(client: httpx.AsyncClient) -> list[str]:
    """Énumère les URLs de fiches via le sitemap WordPress (aycp_ad-sitemap*)."""
    urls: list[str] = []
    try:
        r = await client.get(SITEMAP_INDEX)
        if r.status_code != 200:
            return urls
        sub = re.findall(r"<loc>([^<]*aycp_ad-sitemap[^<]*)</loc>", r.text)
        for sm in sub:
            rr = await client.get(sm)
            if rr.status_code != 200:
                continue
            for loc in re.findall(r"<loc>([^<]+)</loc>", rr.text):
                if "/collection/" in loc and not loc.rstrip("/").endswith("/all"):
                    urls.append(loc)
            await asyncio.sleep(0.2)
    except Exception as e:
        print(f"[PropertyPrestige] Erreur sitemap: {e}")
    return list(dict.fromkeys(urls))


async def _load_cards(client: httpx.AsyncClient) -> dict[str, dict]:
    """Charge toutes les cartes liste (collection 'all', paginée) en dict url→carte."""
    cards: dict[str, dict] = {}
    for page in range(1, 12):
        url = (
            f"{BASE_URL}/collection/all/"
            if page == 1
            else f"{BASE_URL}/collection/all/page/{page}/"
        )
        try:
            r = await client.get(url)
        except Exception:
            break
        if r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        listing = soup.select("div.listing-card")
        if not listing:
            break
        new = 0
        for c in listing:
            parsed = _parse_card(c)
            if parsed and parsed["url"] not in cards:
                cards[parsed["url"]] = parsed
                new += 1
        if new == 0:
            break
        await asyncio.sleep(0.4)
    return cards


def _parse_card(card) -> dict | None:
    a = card.select_one("h2 a[href]")
    if not a:
        return None
    url = a.get("href", "").strip()
    if not url:
        return None
    titre = a.get_text(" ", strip=True)

    price_el = card.select_one(".price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    md_el = card.select_one(".main-data")
    md = md_el.get_text(" ", strip=True) if md_el else ""
    surface = _parse_surface_construite(md)
    surface_terrain = _parse_terrain_ha(md)

    # Description : texte de la carte après les chiffres (extrait "voir plus")
    description = ""
    desc_el = card.select_one(".description, .excerpt, .text")
    if desc_el:
        description = desc_el.get_text(" ", strip=True)

    photos = []
    bg = card.select_one(".background-image")
    if bg:
        style = bg.get("style", "")
        m = re.search(r"url\(['\"]?([^'\")]+)", style)
        if m and m.group(1).startswith("http"):
            photos.append(m.group(1))

    return {
        "url": url,
        "titre": titre,
        "prix": prix,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "description": description,
        "photos": photos,
    }


async def _confirm_detail(
    client: httpx.AsyncClient, url: str, b: dict, departements: set[str]
) -> bool:
    """Visite la fiche : confirme la localisation et complète les champs manquants.
    Retourne True si le bien doit être conservé."""
    r = await client.get(url)
    if r.status_code != 200:
        return True  # déjà matché sur titre → on garde
    soup = BeautifulSoup(r.text, "html.parser")
    full = soup.get_text(" ", strip=True)

    # Ligne de localisation explicite : "Localisation Privilégiée : En <Dept>"
    m_loc = re.search(
        r"Localisation[^:]{0,30}:\s*(?:En|Dans|À|A)?\s*([A-Za-zÀ-ÿ'\- ]{3,40})",
        full,
    )
    if m_loc:
        dept_loc = _match_dept(m_loc.group(1), departements)
        # Si la localisation explicite désigne un AUTRE département cible que le
        # titre, on fait confiance à la localisation (plus précise). Si elle ne
        # matche aucun cible alors que le titre matchait, on garde le titre.
        if dept_loc:
            b["departement"] = dept_loc

    # Ville : on tente d'extraire un nom propre après "à <Ville>" (best effort).
    if not b.get("ville"):
        m_v = re.search(r"\b[àa]\s+([A-ZÀ-Ÿ][a-zà-ÿ\-]{2,30})", full)
        if m_v:
            b["ville"] = m_v.group(1)

    # Description complète (best effort)
    if not b.get("description"):
        desc_el = soup.select_one(".entry-content, .description, article")
        if desc_el:
            b["description"] = desc_el.get_text(" ", strip=True)[:1200]

    # Surface / terrain de secours depuis le texte
    if not b.get("surface"):
        b["surface"] = _parse_surface_construite(full)
    if not b.get("surface_terrain"):
        b["surface_terrain"] = _parse_terrain_ha(full)
    if not b.get("prix"):
        m_p = re.search(r"Prix\s*:?\s*([\d\s\xa0]+)\s*€", full)
        if m_p:
            b["prix"] = _parse_price(m_p.group(1))

    return True


def _finalize(url: str, b: dict) -> dict:
    titre = (b.get("titre") or "").strip()
    return {
        "source": "property_prestige",
        "url": url,
        "id_annonce": url.rstrip("/").split("/")[-1] or url,
        "titre": titre[:150],
        "type_bien": _guess_type(titre) or "propriete",
        "description": (b.get("description") or "")[:1200],
        "departement": b.get("departement", ""),
        "ville": (b.get("ville") or "")[:80],
        "code_postal": "",  # non exposé par le site
        "surface": b.get("surface"),
        "surface_terrain": b.get("surface_terrain"),
        "pieces": None,
        "chambres": None,
        "prix": b.get("prix"),
        "photos": (b.get("photos") or [])[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Property Prestige",
    }


_TYPE_RE = re.compile(
    r"(ch[âa]teau|domaine|propri[ée]t[ée]|manoir|moulin|long[èe]re|ferme|"
    r"demeure|villa|maison|mas|logis|g[îi]te|prieur[ée])",
    re.IGNORECASE,
)


def _guess_type(titre: str) -> str | None:
    m = _TYPE_RE.search(_norm(titre))
    return m.group(1) if m else None


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text or "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface_construite(text: str) -> float | None:
    """'2000 M² Surface (construite)' / '350 M² sur ...' → 2000.0."""
    m = re.search(r"([\d\s\xa0]+)\s*M²\s*(?:Surface|hab|construite)", text or "", re.IGNORECASE)
    if not m:
        m = re.search(r"([\d\s\xa0]+)\s*m²", text or "", re.IGNORECASE)
    if m:
        return _to_float(m.group(1))
    return None


def _parse_terrain_ha(text: str) -> float | None:
    """'60 Ha Surface Terres' / '1,8HA' → 600000.0 m² (1 ha = 10 000 m²)."""
    m = re.search(r"([\d]+(?:[.,]\d+)?)\s*Ha\b", text or "", re.IGNORECASE)
    if m:
        val = m.group(1).replace(",", ".")
        try:
            return round(float(val) * 10000)
        except ValueError:
            return None
    return None


def _to_float(s: str) -> float | None:
    val = re.sub(r"[\s\xa0]", "", s or "")
    try:
        return float(val) if val else None
    except ValueError:
        return None


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent))
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
    print(f"\nTotal PropertyPrestige: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:50]} — {b['prix']}€ — "
            f"{b.get('surface') or '?'}m² — terrain {b.get('surface_terrain') or '?'}m² "
            f"— {b['ville']}"
        )

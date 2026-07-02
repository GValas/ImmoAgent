"""scrapers/timmonivernais.py — T'immo Nivernais (agence locale Nièvre / Morvan)

Méthode : scrape_simple (httpx) — SSR HTML (thème WordPress « WpEstate »).

Couverture : agence indépendante implantée dans la Nièvre (58), spécialisée
Nivernais / Morvan (maisons de campagne, propriétés, presbytères, longères,
domaines avec terrain/étang). Mono-département : tout son inventaire est en
Nièvre → on filtre STRICTEMENT sur le champ détail « State/County: Nièvre »
(= dept 58). Aucune fuite possible vers les autres départements cibles, qu'elle
ne couvre pas.

URL pattern (liste) : /annonces/                (page 1)
                      /annonces/page/{N}/        (pages suivantes)
  → cartes div.property_listing ; on en extrait seulement le lien détail
    (a[href*='/annonces/<slug>/']). Les champs structurés (ville, département,
    surface, terrain, pièces) ne sont PAS dans la carte → page détail requise.

URL pattern (détail) : /annonces/{slug}/
  Lignes « Label: valeur » dans div.listing_detail :
    - City: {ville}
    - State/County: {departement-nom}   → filtre dept (doit valoir « Nièvre »)
    - Property Id : {id}
    - Price: {prix} €
    - Property Size: {surface hab} m2
    - Property Lot Size: {terrain} m2
    - Rooms: {pieces}        Bedrooms: {chambres}
  Titre : h1 ; description : meta og:description ; photos : galerie
  wp-content/uploads (data-original / data-lazy-load-src / src).

Pas de code postal exposé (les nombres à 5 chiffres sont des IDs internes) ;
on renseigne code_postal=None, departement="58", ville=City. La géoloc du
projet exploite par ailleurs les coordonnées (lat/long présentes dans le HTML).

Type de bien : déduit du titre (maison / propriété / presbytère / longère /
domaine / ferme…) ; on exclut les locaux commerciaux / industriels.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.timmonivernais.com"
MAX_PAGES = 8
PHOTOS_PER_BIEN = 12

# Cette agence ne couvre QUE la Nièvre. On ne retient un bien que si la page
# détail confirme State/County == Nièvre → code département 58.
DEPT_COUNTY = {"nievre": "58", "nièvre": "58"}


_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|presbyt|corps de ferme|"
    r"maison de village|maison de campagne|ensemble immobilier|pavillon|gite|gîte",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"local|commercial|industriel|hangar|entrep[oô]t|fonds de commerce|"
    r"bureau|garage|parking|terrain (?:à|a) b[aâ]tir|appartement|immeuble",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # L'agence est en Nièvre : si 58 n'est pas demandé, rien à scraper.
    if departements and "58" not in departements:
        print("[TimmoNivernais] Dept 58 hors cibles → 0 annonce")
        return []

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        detail_urls = await _collect_detail_urls(client)
        print(f"[TimmoNivernais] {len(detail_urls)} annonces listées")

        seen: set[str] = set()
        for url in detail_urls:
            if url in seen:
                continue
            seen.add(url)
            try:
                bien = await _parse_detail(client, url)
            except Exception as e:
                print(f"[TimmoNivernais] Erreur détail {url}: {e}")
                bien = None
            if not bien:
                continue

            # Filtre département STRICT (0 fuite) : doit être Nièvre / 58
            if bien["departement"] != "58":
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

    print(f"[TimmoNivernais] Dept 58: {len(results)} annonces retenues")
    return results


async def _collect_detail_urls(client: httpx.AsyncClient) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    pat = re.compile(r"/annonces/[a-z0-9-]+/?$")

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/annonces/" if page == 1 else f"{BASE_URL}/annonces/page/{page}/"
        r = await client.get(url)
        if r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        found = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.startswith("http"):
                href = BASE_URL + href
            # Normalise (enlève querystring/ancre)
            href = href.split("?")[0].split("#")[0]
            if not href.startswith(BASE_URL + "/annonces/"):
                continue
            tail = href[len(BASE_URL):]
            if not pat.search(tail):
                continue
            if "/page/" in tail:
                continue
            if href in seen:
                continue
            seen.add(href)
            urls.append(href)
            found += 1
        if found == 0:
            break
        await asyncio.sleep(0.4)

    return urls


async def _parse_detail(client: httpx.AsyncClient, url: str) -> dict | None:
    r = await client.get(url)
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    # Titre
    h1 = soup.select_one("h1")
    titre = h1.get_text(" ", strip=True) if h1 else ""

    # Type de bien (depuis titre) : exclure locaux commerciaux/industriels
    if _EXCLUDE_TYPE.search(titre) and not _KEEP_TYPE.search(titre):
        return None
    type_bien = _guess_type(titre)

    # Lignes « Label: valeur »
    fields = _parse_listing_details(soup)

    # Département via State/County
    county = (fields.get("state/county") or fields.get("state") or "").strip().lower()
    departement = DEPT_COUNTY.get(county)
    if departement is None:
        # On n'accepte un bien que si le département est explicitement la Nièvre.
        return None

    ville = (fields.get("city") or "").strip()

    id_annonce = (
        fields.get("property id")
        or fields.get("propertyid")
        or url.rstrip("/").split("/")[-1]
    )

    prix = _parse_num(fields.get("price"))
    surface = _parse_num(fields.get("property size"))
    surface_terrain = _parse_num(fields.get("property lot size"))
    pieces = _parse_int(fields.get("rooms"))
    chambres = _parse_int(fields.get("bedrooms"))

    # Description : og:description
    description = ""
    og = soup.select_one('meta[property="og:description"]')
    if og and og.get("content"):
        description = og["content"].strip()
    if not description:
        cd = soup.select_one("#description, .panel-body, .listing_details")
        if cd:
            description = cd.get_text(" ", strip=True)

    # Photos : galerie wp-content/uploads
    photos = _extract_photos(soup)

    return {
        "source": "timmonivernais",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": departement,
        "ville": ville[:80],
        "code_postal": None,  # non exposé par le site (IDs ≠ CP)
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "T'immo Nivernais",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_listing_details(soup) -> dict:
    """Transforme les lignes div.listing_detail « Label: valeur » en dict."""
    out: dict[str, str] = {}
    for el in soup.select(".listing_detail"):
        txt = el.get_text(" ", strip=True)
        if ":" not in txt:
            continue
        label, _, value = txt.partition(":")
        key = re.sub(r"\s+", " ", label).strip().lower()
        out[key] = value.strip()
    return out


def _guess_type(titre: str) -> str:
    t = titre.lower()
    for kw, label in (
        ("presbyt", "presbytère"),
        ("château", "château"),
        ("chateau", "château"),
        ("manoir", "manoir"),
        ("moulin", "moulin"),
        ("longere", "longère"),
        ("longère", "longère"),
        ("ferme", "ferme"),
        ("domaine", "domaine"),
        ("propri", "propriété"),
        ("maison de village", "maison de village"),
        ("maison de campagne", "maison de campagne"),
        ("ensemble immobilier", "ensemble immobilier"),
        ("villa", "villa"),
        ("pavillon", "pavillon"),
        ("maison", "maison"),
    ):
        if kw in t:
            return label
    return "maison"


def _parse_num(text) -> float | None:
    """'349,000 €' → 349000.0 ; '85,665 m 2' → 85665.0 ; '110 m 2' → 110.0

    Attention : la surface est rendue « 110 m 2 » (le <sup>2</sup> de m² colle un
    '2' parasite). On coupe donc la chaîne à la 1ʳᵉ unité ('m', '€') avant de
    retirer les séparateurs de milliers (virgule/espace)."""
    if not text:
        return None
    s = str(text)
    m = re.search(r"[\d][\d\s\xa0,.]*", s)
    if not m:
        return None
    num = m.group(0)
    # Sépare le 1ᵉʳ bloc numérique (avant l'unité) : on coupe à la 1ʳᵉ espace
    # qui précède une unité éventuelle (… 110 m 2 → '110').
    num = num.split("m")[0]
    cleaned = re.sub(r"[\s\xa0,]", "", num)
    cleaned = re.sub(r"\.(?=\d{3}\b)", "", cleaned)  # point séparateur de milliers
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(text) -> int | None:
    if not text:
        return None
    m = re.search(r"\d+", str(text))
    return int(m.group(0)) if m else None


def _extract_photos(soup) -> list[str]:
    photos: list[str] = []
    seen: set[str] = set()
    for img in soup.find_all("img"):
        src = (
            img.get("data-original")
            or img.get("data-lazy-load-src")
            or img.get("data-src")
            or img.get("src")
            or ""
        )
        if not src or "wp-content/uploads" not in src:
            continue
        if src.endswith(".svg") or src.startswith("data:"):
            continue
        # Retire le suffixe de taille -WxH pour la pleine résolution
        full = re.sub(r"-\d+x\d+(?=\.\w+$)", "", src)
        if full in seen:
            continue
        seen.add(full)
        photos.append(full)
        if len(photos) >= PHOTOS_PER_BIEN:
            break
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
    print(f"\nTotal T'immo Nivernais: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
            f" — {len(b['photos'])} photos"
        )

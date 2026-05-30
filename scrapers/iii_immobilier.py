"""
scrapers/iii_immobilier.py — Agence Pollet / iii-immobilier (rural Cher / Sologne)
Méthode : httpx pur — SSR HTML (PHP)
Approche (calquée sur patrice_besse.py — petit inventaire curated) :
  1. Pagine /tous-les-biens-liste.php?page=N (cartes a.prop-card) → liste de toutes
     les annonces (~50 biens, /bien/{id}/{slug}).
  2. Fetche chaque fiche : JSON-LD RealEstateListing (prix, description) + table
     .fiche-features (Catégorie, Surface, Terrain, Chambres, DPE, Réf) + carte
     Leaflet `data-lat/data-lng` (centre secteur approximatif).
  3. FILTRE DÉPARTEMENT — le site n'expose AUCUN code postal/commune précis :
     - addressLocality JSON-LD = libellé secteur ("Sologne", "Sancerrois"…)
     - data-lat/lng = centre du secteur (rayon flouté 7–30 km)
     On REVERSE-GÉOCODE ces coordonnées via geo.api.gouv.fr (gratuit, officiel)
     → commune + code postal + codeDepartement RÉELS. Le département obtenu est
     l'autorité du post-filtre (0 fuite). Repli : mapping secteur→dept si l'API
     échoue. Les coords étant centrées secteur, ville/CP sont approximatifs mais le
     département est fiable (sectors centrés bien à l'intérieur de leur dept/zone).
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re
import json
import httpx
from bs4 import BeautifulSoup

BASE = "https://www.iii-immobilier.fr"
LISTING = BASE + "/tous-les-biens-liste.php"
GEO_API = "https://geo.api.gouv.fr/communes"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

MAX_PAGES = 12               # garde-fou pagination
MAX_INDIVIDUAL_FETCHES = 60  # inventaire curated (~50 biens) — couvre tout

# Repli si reverse-geocode indisponible : libellé secteur → dept dominant.
# (L'agence est à Aubigny-sur-Nère 18 ; couverture Cher / Sologne / Loiret.)
_SECTOR_DEPT_FALLBACK: dict[str, str] = {
    "aubigny": "18",
    "sancerr": "18",
    "bourges": "18",
    "cher": "18",
    "berry": "18",
    "sologne": "41",
    "loiret": "45",
    "loir-et-cher": "41",
}

# Catégories iii-immobilier → type_bien normalisé
_TYPE_MAP = {
    "maison": "maison",
    "propriété": "maison",
    "propriete": "maison",
    "demeure": "maison",
    "domaine": "maison",
    "manoir": "maison",
    "château": "maison",
    "chateau": "maison",
    "terrain": "terrain",
    "appartement": "appartement",
}


def _sector_fallback_dept(label: str) -> str:
    low = (label or "").lower()
    for k, d in _SECTOR_DEPT_FALLBACK.items():
        if k in low:
            return d
    return ""


def _int_from(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"([\d][\d\s \.]*\d|\d)", text.replace(" ", " "))
    if not m:
        return None
    try:
        return int(re.sub(r"[^\d]", "", m.group(1)))
    except Exception:
        return None


def _collect_listing(html: str) -> dict[str, str]:
    """Retourne {bien_id: slug} depuis une page de listing."""
    out: dict[str, str] = {}
    for m in re.finditer(r"/bien/(\d+)/([a-z0-9\-]+)", html):
        out.setdefault(m.group(1), m.group(2))
    return out


def _parse_features(soup: BeautifulSoup) -> dict[str, str]:
    feats: dict[str, str] = {}
    table = soup.select_one("table.fiche-features")
    if not table:
        return feats
    for tr in table.select("tr"):
        th = tr.find("th")
        td = tr.find("td")
        if th and td:
            feats[th.get_text(" ", strip=True).lower()] = td.get_text(" ", strip=True)
    return feats


def _parse_fiche(html: str, bid: str, slug: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")

    # ── JSON-LD RealEstateListing ──
    ld = None
    for sc in soup.select('script[type="application/ld+json"]'):
        if not sc.string or "RealEstateListing" not in sc.string:
            continue
        try:
            ld = json.loads(sc.string)
            break
        except Exception:
            pass
    ld = ld or {}

    # ── Prix (offers.price, clean int) ──
    prix = None
    offers = ld.get("offers") or {}
    if offers.get("price"):
        try:
            prix = float(re.sub(r"[^\d]", "", str(offers["price"])))
        except Exception:
            prix = None

    feats = _parse_features(soup)

    # Repli prix : "Hors honoraires" ou premier prix de la fiche
    if not prix:
        m = re.search(r"([\d][\d\s ]{4,})\s*€", html.replace(" ", " "))
        if m:
            prix = _int_from(m.group(1))
    if not prix or prix < 10_000:
        return None

    # ── Titre ──
    h1 = soup.select_one("h1.fiche-head__title, h1")
    titre = (h1.get_text(" ", strip=True) if h1 else ld.get("name", ""))[:160] or "Bien rural"

    description = (ld.get("description") or "")[:1500]

    # ── Type ──
    cat = feats.get("catégorie", feats.get("categorie", ""))
    type_bien = "maison"
    for k, v in _TYPE_MAP.items():
        if k in cat.lower():
            type_bien = v
            break

    # ── Surface / terrain / chambres ──
    surface = _int_from(feats.get("surface habitable", ""))
    surface_terrain = _int_from(feats.get("surface terrain", ""))
    chambres = _int_from(feats.get("chambres", ""))

    # ── DPE ──
    dpe = None
    dpe_raw = feats.get("dpe", "")
    m = re.match(r"\s*([A-G])\b", dpe_raw)
    if m:
        dpe = m.group(1)

    # ── Référence ──
    ref = feats.get("référence", feats.get("reference", "")) or bid

    # ── Secteur + coordonnées approximatives ──
    locality = ((ld.get("address") or {}).get("addressLocality")) or feats.get("secteur", "")
    el = soup.select_one("[data-lat][data-lng]")
    lat = lng = None
    if el:
        try:
            lat = float(el.get("data-lat"))
            lng = float(el.get("data-lng"))
        except Exception:
            lat = lng = None

    # ── Photos ──
    photos = []
    for img in ld.get("image", []) if isinstance(ld.get("image"), list) else []:
        if isinstance(img, str) and img.startswith("http"):
            photos.append(img)
    photos = list(dict.fromkeys(photos))[:10]

    has_pool = bool(re.search(r"\bpiscine\b", (titre + " " + description), re.IGNORECASE))

    return {
        "source": "iii_immobilier",
        "url": f"{BASE}/bien/{bid}/{slug}",
        "id_annonce": str(ref),
        "titre": titre,
        "type_bien": type_bien,
        "description": description,
        "departement": "",          # rempli après reverse-geocode
        "ville": locality,          # secteur (approx) ; affiné par geocode
        "code_postal": "",          # rempli après reverse-geocode
        "surface": float(surface) if surface else None,
        "surface_terrain": float(surface_terrain) if surface_terrain else None,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Agence Pollet (iii-immobilier)",
        "has_pool": has_pool,
        "latitude": lat,
        "longitude": lng,
        "_sector": locality,
    }


async def _reverse_geocode(client: httpx.AsyncClient, lat: float, lng: float) -> dict | None:
    """Coords (centre secteur) → commune/CP/dept réels via geo.api.gouv.fr."""
    try:
        r = await client.get(
            GEO_API,
            params={
                "lat": f"{lat}",
                "lon": f"{lng}",
                "fields": "nom,codeDepartement,codesPostaux",
                "format": "json",
            },
            timeout=15,
        )
        if r.status_code != 200:
            return None
        j = r.json()
        if not j:
            return None
        c = j[0]
        cps = c.get("codesPostaux") or []
        return {
            "ville": c.get("nom", ""),
            "departement": c.get("codeDepartement", ""),
            "code_postal": cps[0] if cps else "",
        }
    except Exception:
        return None


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max    = criteres.get("prix_max", 0)
    prix_min    = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    biens: list[dict] = []

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=30) as client:
        # ── Étape 1 : pagination du listing global ──
        inventory: dict[str, str] = {}
        for page in range(1, MAX_PAGES + 1):
            try:
                r = await client.get(LISTING, params={"page": page})
            except Exception as e:
                print(f"[IIIImmobilier] ERR page {page}: {e}")
                break
            if r.status_code != 200:
                break
            found = _collect_listing(r.text)
            new = {k: v for k, v in found.items() if k not in inventory}
            if not new and page > 1:
                break
            inventory.update(new)
            if not found:
                break
        print(f"[IIIImmobilier] {len(inventory)} biens au catalogue")
        if not inventory:
            return []

        # ── Étape 2 : fiches individuelles ──
        items = list(inventory.items())[:MAX_INDIVIDUAL_FETCHES]
        for bid, slug in items:
            try:
                r2 = await client.get(f"{BASE}/bien/{bid}/{slug}")
                if r2.status_code != 200:
                    continue
                b = _parse_fiche(r2.text, bid, slug)
            except Exception as e:
                print(f"[IIIImmobilier] ERR fiche {bid}: {e}")
                continue
            if not b:
                continue

            # ── Étape 3 : département via reverse-geocode des coords secteur ──
            dept = ""
            geo = None
            if b.get("latitude") is not None and b.get("longitude") is not None:
                geo = await _reverse_geocode(client, b["latitude"], b["longitude"])
            if geo and geo.get("departement"):
                dept = geo["departement"]
                b["departement"] = dept
                b["code_postal"] = geo.get("code_postal", "")
                if geo.get("ville"):
                    b["ville"] = geo["ville"]
            else:
                dept = _sector_fallback_dept(b.get("_sector", ""))
                b["departement"] = dept

            b.pop("_sector", None)

            # ── Post-filtre département (0 fuite) ──
            if departements:
                if not dept or dept not in departements:
                    continue

            # ── Filtres prix / surface ──
            if prix_max and b.get("prix") and b["prix"] > prix_max:
                continue
            if prix_min and b.get("prix") and b["prix"] < prix_min:
                continue
            if surface_min and b.get("surface") and b["surface"] < surface_min:
                continue

            biens.append(b)
            print(
                f"[IIIImmobilier] ✓ {b['titre'][:55]} — {b['prix']:.0f}€ — "
                f"{b['surface']}m² — {b['ville']} ({b['departement']})"
            )
            await asyncio.sleep(0.2)

    print(f"[IIIImmobilier] total: {len(biens)} biens")
    return biens


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    try:
        from config_loader import load_criteria
        crit = load_criteria()
        prix_max, prix_min, surface_min = crit.prix_max, crit.prix_min, crit.surface_min
    except Exception:
        prix_max, prix_min, surface_min = 0, 0, 0

    TARGET = [72, 28, 45, 89, 49, 37, 36, 18, 58, 41, 53]

    async def _test():
        result = await search({
            "departements": TARGET,
            "prix_max": prix_max,
            "prix_min": prix_min,
            "surface_min": surface_min,
        })
        print(f"\nTotal: {len(result)} annonces")
        from collections import Counter
        by_dept = Counter(b["departement"] for b in result)
        for d in sorted(by_dept):
            print(f"  dept {d}: {by_dept[d]}")
        leaks = [b for b in result if b["departement"] not in {str(x).zfill(2) for x in TARGET}]
        print(f"FUITES hors-cible: {len(leaks)}")
        for b in result[:8]:
            print(f"  {b['titre'][:60]} — {b['prix']:.0f}€ — {b['surface']}m² — "
                  f"{b['ville']} ({b['code_postal']}/{b['departement']})")

    asyncio.run(_test())

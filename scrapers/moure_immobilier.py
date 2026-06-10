"""scrapers/moure_immobilier.py — Moure Immobilier (agence locale, Montargis 45)

Méthode : scrape_simple (httpx) — SSR HTML statique (CMS Contao / MetaModels)
URL pattern :
  - Liste  : /annonces-immobilieres.html  (une seule page, ~9 biens, pas de pagination)
  - Détail : /annonce-immobiliere/{id-slug}.html

Filtre département : l'agence est mono-implantation (Montargis, Loiret). Toutes les
annonces sont dans le 45, mais on applique malgré tout un POST-FILTRE STRICT sur le
code département extrait de la localité « Ville (45) » → 0 fuite hors-zone garantie.

Cartes liste (div.item) :
  - URL    : a[href*="/annonce-immobiliere/"]
  - Titre  : .field.Titre        → "Maison", "Fermette", "Immeuble de rapport"…
  - Réf    : .field.reference     → "4498"
  - Localité: .field.localite     → "Chapelon (45). 14 km Montargis. 1h30 Paris Sud"
  - Prix   : .field.prix          → "315 000"
  - Photo  : .field.photo img/source (relative assets/images/…)

Enrichissement page détail :
  - .field.descriptif → surface habitable, chambres, terrain, description complète
  - code postal complet (\\b45\\d{3}\\b) si présent
  - DPE via classe CSS  mdpe-{LETTRE}  (ou  field dpe-{LETTRE})
  - galerie photos complète (assets/images/…)

Types conservés : maisons / pavillons / fermettes / granges / propriétés ;
terrains / immeubles de rapport exclus.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.moure.fr"
LIST_URL = f"{BASE_URL}/annonces-immobilieres.html"
PHOTOS_PER_CARD = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien à conserver / exclure (déduits du titre)
_KEEP_TYPE = re.compile(
    r"maison|pavillon|fermette|ferme|grange|propri[eé]t[eé]|villa|longere|longère|"
    r"manoir|chateau|château|moulin|demeure|domaine|mas|corps de ferme|maison de ville",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"terrain|immeuble|local|commerce|garage|parking|bureau|fonds|appartement",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        try:
            r = await client.get(LIST_URL)
        except Exception as e:
            print(f"[Moure] Erreur réseau liste : {e}")
            return results
        if r.status_code != 200:
            print(f"[Moure] Liste HTTP {r.status_code}")
            return results

        items = BeautifulSoup(r.text, "html.parser").select("div.item")
        seen: set[str] = set()

        for item in items:
            try:
                card = _parse_card(item)
            except Exception:
                continue
            if not card:
                continue

            dept = card["departement"]
            # POST-FILTRE STRICT : on n'accepte que les départements cibles
            if dept not in departements:
                continue

            aid = card["id_annonce"]
            if aid in seen:
                continue
            seen.add(aid)

            # Enrichissement page détail (surface, terrain, chambres, CP, DPE, photos)
            try:
                await _enrich_detail(client, card)
            except Exception:
                pass

            # Re-vérification du département après enrichissement (CP complet éventuel)
            if card["code_postal"] and card["code_postal"][:2] != dept:
                continue

            p = card.get("prix") or 0
            s = card.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            results.append(card)
            await asyncio.sleep(0.5)

    print(f"[Moure] {len(results)} annonces (zone cible)")
    return results


def _parse_card(item) -> dict | None:
    link = item.select_one("a[href*='/annonce-immobiliere/']")
    href = link.get("href", "") if link else ""
    if not href or href.rstrip("/").endswith("/annonce-immobiliere.html"):
        # lien générique sans slug → ignorer
        return None
    url = href if href.startswith("http") else BASE_URL + "/" + href.lstrip("/")

    title_el = item.select_one(".field.Titre")
    type_raw = title_el.get_text(" ", strip=True) if title_el else ""
    if _EXCLUDE_TYPE.search(type_raw) and not _KEEP_TYPE.search(type_raw):
        return None
    if not _KEEP_TYPE.search(type_raw):
        return None
    type_bien = type_raw.strip() or "maison"

    ref_el = item.select_one(".field.reference")
    ref = ref_el.get_text(strip=True) if ref_el else ""

    loc_el = item.select_one(".field.localite")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, dept = _parse_loc(loc)
    if not dept:
        return None

    price_el = item.select_one(".field.prix")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # id_annonce : ref si dispo, sinon slug d'URL
    id_annonce = ref or url.rsplit("/", 1)[-1].replace(".html", "") or url

    titre = f"{type_bien} {ville}".strip()

    photos = _collect_photos(item)

    return {
        "source": "moure_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien.lower(),
        "description": loc[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",
        "surface": None,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Moure Immobilier",
    }


async def _enrich_detail(client: httpx.AsyncClient, card: dict) -> None:
    r = await client.get(card["url"])
    if r.status_code != 200:
        return
    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    desc_el = soup.select_one(".field.descriptif")
    descriptif = desc_el.get_text(" ", strip=True) if desc_el else ""
    if descriptif:
        card["description"] = descriptif[:1200]
        card["surface"] = _parse_surface_hab(descriptif)
        card["surface_terrain"] = _parse_terrain(descriptif)
        card["chambres"] = _parse_int(r"(\d+)\s*chambres?", descriptif)

    # Code postal complet, validé contre le département déjà connu
    for cp in re.findall(r"\b(\d{5})\b", html):
        if cp[:2] == card["departement"]:
            card["code_postal"] = cp
            break

    # DPE : classe CSS  mdpe-{LETTRE}  ou  field dpe-{LETTRE}
    card["dpe"] = _parse_dpe(soup)

    photos = _collect_photos(soup)
    if photos:
        card["photos"] = photos


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Chapelon (45). 14 km Montargis…' → ('Chapelon', '45')"""
    dept = ""
    m = re.search(r"\((\d{2,3})\)", text)
    if m:
        dept = m.group(1).zfill(2)[:2]
    # ville = ce qui précède la parenthèse
    ville = text.split("(")[0].strip(" .-")
    return ville, dept


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_surface_hab(text: str) -> float | None:
    """'202 m² habitables' / '120 m2 habitable' → float."""
    if not text:
        return None
    m = re.search(
        r"(\d[\d\s\xa0.,]*)\s*m[²2]\s*(?:hab|habitable)", text, re.IGNORECASE
    )
    if not m:
        # repli : premier 'NN m²' du descriptif
        m = re.search(r"(\d[\d\s\xa0]*)\s*m[²2]\b", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
        # garder uniquement la partie entière si point milliers
        try:
            f = float(val)
            if 8 <= f <= 3000:
                return f
        except ValueError:
            pass
    return None


def _parse_terrain(text: str) -> float | None:
    """'Terrain clos de 1.322 m²' → 1322.0  (le '.' est un séparateur de milliers)."""
    m = re.search(r"[Tt]errain[^0-9]*([\d\s\xa0.,]+)\s*m[²2]", text)
    if not m:
        return None
    raw = m.group(1).strip()
    # supprime espaces/insécables ; '.' ou ',' = séparateur milliers ici
    digits = re.sub(r"[\s\xa0.,]", "", raw)
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


def _parse_dpe(soup) -> str | None:
    """DPE depuis classe CSS mdpe-{L} ou field dpe-{L} (L ∈ A..G)."""
    for el in soup.select("[class]"):
        for cls in el.get("class", []):
            m = re.match(r"(?:m?dpe)-([A-G])$", cls)
            if m:
                return m.group(1)
    return None


def _collect_photos(node) -> list[str]:
    photos: list[str] = []
    seen: set[str] = set()
    for el in node.select("source[srcset], img[src]"):
        u = el.get("srcset") or el.get("src") or ""
        u = u.split()[0] if u else ""
        if not u or u.startswith("data:"):
            continue
        if "assets/images" not in u and "/files/moure" not in u:
            continue
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            u = BASE_URL + u
        elif not u.startswith("http"):
            u = BASE_URL + "/" + u
        if u not in seen:
            seen.add(u)
            photos.append(u)
        if len(photos) >= PHOTOS_PER_CARD:
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
    print(f"\nTotal Moure Immobilier: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal'] or b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — DPE {b.get('dpe') or '?'}"
            f" — {len(b['photos'])} photos — {b['ville']}"
        )

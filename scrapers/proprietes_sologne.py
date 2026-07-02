"""scrapers/proprietes_sologne.py — Propriétés de Sologne (agence spécialisée)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress / thème EstateAgent "es-*")
URL pattern :
  - Liste complète : sitemap dédié /wp-sitemap-posts-properties-1.xml
    → toutes les fiches /property/{slug}/ (source de vérité, ~73 biens)
  - Détail : /property/{slug}/ — contient un bloc JSON-LD schema.org/House
    (address "CP, Ville", numberOfBedrooms, description, image[]) + des champs
    .es-property-field (Nombre de pièces, Salles de bains, Superficie) + le prix
    .es-price.

Filtre département : agence mono-zone (Sologne, cœur 41 Loir-et-Cher, déborde un
  peu sur 18/36/37/45). PAS de filtre serveur par dept → on récupère tout puis
  on POST-FILTRE strict sur code_postal[:2] ∈ départements cibles. 0 fuite.

Types : on exclut appartement / terrain / immeuble / entrepôt / investissement /
  fonds / parking via le slug d'URL et la catégorie ; on garde maison, propriété,
  longère, ferme, manoir, château, domaine, villa, gîte.

Cartes/champs détail :
  - JSON-LD : name, address ("41230, Mur-en-Sologne"), numberOfBedrooms,
              numberOfBathroomsTotal, description, image[]
  - Prix    : <span class="es-price">445,200€</span>
  - Pièces  : champ "Nombre de pièces"
  - Surface : champ "Superficie" → <b>233</b> m²

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import html
import json
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://proprietes-sologne.com"
SITEMAP_URL = f"{BASE_URL}/wp-sitemap-posts-properties-1.xml"
PHOTOS_PER_BIEN = 10
CONCURRENCY = 6


# Types conservés (maisons / propriétés / vieilles pierres)
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|fermette|longere|longère|manoir|"
    r"chateau|château|moulin|demeure|domaine|gite|gîte|bord-de|borde",
    re.IGNORECASE,
)
# Types explicitement exclus (slug d'URL)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|immeuble|entrepot|entrepôt|investissement|fonds|"
    r"local|commerce|garage|parking|bureau|territoire-de-chasse|etang",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        try:
            urls = await _list_property_urls(client)
        except Exception as e:
            print(f"[ProprietesSologne] Erreur sitemap: {e}")
            return results

        print(f"[ProprietesSologne] {len(urls)} fiches dans le sitemap")

        # Pré-filtre des types non pertinents via le slug (économie de requêtes)
        urls = [u for u in urls if _slug_keep(u)]
        print(f"[ProprietesSologne] {len(urls)} fiches après filtre type (slug)")

        sem = asyncio.Semaphore(CONCURRENCY)

        async def _fetch(u: str) -> dict | None:
            async with sem:
                try:
                    bien = await _parse_detail(client, u)
                except Exception as e:
                    print(f"[ProprietesSologne] Erreur fiche {u}: {e}")
                    return None
                await asyncio.sleep(0.4)
                return bien

        biens = await asyncio.gather(*[_fetch(u) for u in urls])

    for bien in biens:
        if not bien:
            continue
        cp = bien.get("code_postal") or ""
        # Post-filtre département STRICT — 0 fuite hors-zone
        if not cp or cp[:2] not in departements:
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

    print(f"[ProprietesSologne] {len(results)} biens retenus (zone + bornes)")
    return results


# ── Sitemap ───────────────────────────────────────────────────────────────────

async def _list_property_urls(client: httpx.AsyncClient) -> list[str]:
    r = await client.get(SITEMAP_URL)
    r.raise_for_status()
    urls = re.findall(
        r"<loc>(" + re.escape(BASE_URL) + r"/property/[^<]+)</loc>", r.text
    )
    # dédup en gardant l'ordre
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _slug_keep(url: str) -> bool:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    if _EXCLUDE_TYPE.search(slug):
        return False
    # On garde si type reconnu, sinon on garde par défaut (le détail tranchera)
    return True


# ── Détail ──────────────────────────────────────────────────────────────────

async def _parse_detail(client: httpx.AsyncClient, url: str) -> dict | None:
    r = await client.get(url)
    if r.status_code != 200:
        return None
    t = r.text
    soup = BeautifulSoup(t, "html.parser")

    ld = _extract_jsonld(t)
    name = (ld.get("name") or "").strip() if ld else ""
    address = (ld.get("address") or "").strip() if ld else ""
    description = (ld.get("description") or "").strip() if ld else ""

    # Localisation depuis l'adresse JSON-LD "41230, Mur-en-Sologne"
    code_postal, ville = _parse_address(address)
    if not code_postal:
        # secours : chercher un CP dans le titre/description
        m = re.search(r"\b(\d{5})\b", t)
        code_postal = m.group(1) if m else ""

    # Type de bien depuis le slug
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    type_bien = _type_from_slug(slug)
    if _EXCLUDE_TYPE.search(slug):
        return None

    # Prix
    prix = None
    price_el = soup.select_one(".es-price")
    if price_el:
        prix = _parse_price(price_el.get_text(" ", strip=True))

    # Champs es-property-field : Nombre de pièces / Salles de bains / Superficie
    fields_txt = {}
    for li in soup.select(".es-property-field"):
        label_el = li.select_one(".es-property-field__label")
        val_el = li.select_one(".es-property-field__value")
        if label_el and val_el:
            fields_txt[label_el.get_text(strip=True).lower()] = val_el.get_text(
                " ", strip=True
            )

    pieces = _field_int(fields_txt, "nombre de pièces")
    chambres = _field_int(fields_txt, "chambres")
    if chambres is None and ld:
        chambres = _to_int(ld.get("numberOfBedrooms"))
    surface = _field_surface(fields_txt, "superficie")
    if surface is None:
        surface = _parse_surface_from_text(description) or _parse_surface_from_text(name)

    surface_terrain = _parse_terrain_from_text(description)

    # Photos : JSON-LD image[] sinon balises img du carrousel
    photos: list[str] = []
    if ld and isinstance(ld.get("image"), list):
        photos = [u for u in ld["image"] if isinstance(u, str)][:PHOTOS_PER_BIEN]
    if not photos:
        for img in soup.select("img[data-lazy], img[data-src]"):
            src = img.get("data-lazy") or img.get("data-src") or ""
            if src.startswith("http") and "/uploads/" in src:
                photos.append(src)
        photos = photos[:PHOTOS_PER_BIEN]

    titre = html.unescape(name) if name else f"{type_bien.title()} {ville}".strip()
    description = html.unescape(description)
    id_annonce = slug

    departement = code_postal[:2] if code_postal else None

    return {
        "source": "proprietes_sologne",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": departement,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Propriétés de Sologne",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_jsonld(html: str) -> dict | None:
    for m in re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S
    ):
        try:
            data = json.loads(m)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if isinstance(it, dict) and it.get("@type") in (
                "House",
                "Residence",
                "Product",
                "Place",
                "SingleFamilyResidence",
                "Apartment",
            ):
                return it
        # à défaut renvoyer le 1er dict avec une address
        for it in items:
            if isinstance(it, dict) and it.get("address"):
                return it
    return None


def _parse_address(address: str) -> tuple[str, str]:
    """'41230, Mur-en-Sologne' → ('41230', 'Mur-en-Sologne')"""
    if not address:
        return "", ""
    cp = ""
    m = re.search(r"\b(\d{5})\b", address)
    if m:
        cp = m.group(1)
    ville = re.sub(r"\b\d{5}\b", "", address).strip(" ,").strip()
    return cp, ville


def _type_from_slug(slug: str) -> str:
    base = re.sub(r"-\d+$", "", slug)  # retire suffixe -2, -3...
    word = base.split("-")[0]
    mapping = {
        "propriete": "propriété",
        "longere": "longère",
        "fermette": "fermette",
        "ferme": "ferme",
        "maison": "maison",
        "villa": "villa",
        "manoir": "manoir",
        "chateau": "château",
        "domaine": "domaine",
        "moulin": "moulin",
        "gites": "gîte",
        "bord": "propriété",
    }
    return mapping.get(word, "maison")


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    # format "445,200€" → la virgule est séparateur de milliers ici
    cleaned = cleaned.replace(",", "").replace(".", "")
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    if v is not None and v < 1000:  # garde-fou (prix aberrant)
        return None
    return v


def _to_int(val) -> int | None:
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return None


def _field_int(fields: dict, key: str) -> int | None:
    for k, v in fields.items():
        if key in k:
            m = re.search(r"(\d+)", v)
            if m:
                return int(m.group(1))
    return None


def _field_surface(fields: dict, key: str) -> float | None:
    for k, v in fields.items():
        if key in k:
            m = re.search(r"(\d[\d\s\xa0]*)", v)
            if m:
                val = re.sub(r"[\s\xa0]", "", m.group(1))
                try:
                    f = float(val)
                    if 8 <= f <= 5000:
                        return f
                except ValueError:
                    pass
    return None


def _parse_surface_from_text(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0]{1,5})\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 2000:
                return f
        except ValueError:
            pass
    return None


def _parse_terrain_from_text(text: str) -> float | None:
    if not text:
        return None
    # hectares → m²
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*hectare", text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(",", ".")) * 10000
        except ValueError:
            pass
    m = re.search(r"terrain[^0-9]{0,20}(\d[\d\s\xa0]{2,6})\s*m²", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            return float(val)
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
    print(f"\nTotal Propriétés de Sologne: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

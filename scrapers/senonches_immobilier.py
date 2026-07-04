"""scrapers/senonches_immobilier.py — Senonches Immobilier (Senonches, Perche, 28)

Méthode : API JSON (httpx) — le site est un... Shopify : chaque annonce est un
« produit », l'inventaire complet est servi par l'endpoint public
/products.json?limit=250 (1 requête, ~102 produits observés, 84 en dept 28).
Agence indépendante du Perche/Thymerais : longères percheronnes, manoirs,
corps de ferme, maisons de maître.

Champs : prix = variants[0].price ; CP dans le titre « ... à Manou (28240) » ou
dans le body_html ; ville via tags[0] ou le titre ; photos = images[].src.
Les biens VENDU / SOUS-COMPROMIS / À LOUER sont écartés (titre), ainsi que les
types non-maison. Stock mono-secteur avec franges 61/27 → post-filtre STRICT
code_postal[:2] ∈ départements demandés (0 fuite).

Ne requête que si 28 est demandé.

Interface : async def search(criteres: dict) -> list[dict]
"""
import re
import unicodedata

from scrapers._base import (
    get_with_retry,
    keep_bien,
    make_client,
    parse_int,
    standalone_main,
)

BASE_URL = "https://senonches-immobilier.fr"
DEPTS_AGENCE = {"28"}   # secteur Perche/Thymerais (franges 61/27 exclues par CP)
MAX_JSON_PAGES = 4      # 250 produits/page — large

_EXCLUDE_STATUS = re.compile(r"vendu|sous.compromis|a louer|loue", re.IGNORECASE)
_KEEP_TYPES = ("maison",)   # product_type Shopify : Maison / Appartement / Terrain...


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


def _strip_html(html: str) -> str:
    txt = re.sub(r"<style[^>]*>.*?</style>", " ", html or "", flags=re.S | re.I)
    txt = re.sub(r"<script[^>]*>.*?</script>", " ", txt, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def _surface_habitable(txt: str) -> float | None:
    """Première surface « NNN m² » plausible NON précédée de terrain/parcelle/jardin."""
    for m in re.finditer(r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m[²2]\b", txt or ""):
        avant = (txt[max(0, m.start() - 25):m.start()]).lower()
        if "terrain" in avant or "parcelle" in avant or "jardin" in avant:
            continue
        try:
            v = float(re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", "."))
        except ValueError:
            continue
        if 8 <= v <= 1500:
            return v
    return None


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    cibles = departements & DEPTS_AGENCE
    if not cibles:
        return []

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    biens: list[dict] = []
    seen_ids: set[str] = set()

    async with make_client() as client:
        for page in range(1, MAX_JSON_PAGES + 1):
            r = await get_with_retry(
                client, f"{BASE_URL}/products.json?limit=250&page={page}")
            if r is None or r.status_code != 200:
                break
            products = r.json().get("products", [])
            if not products:
                break
            for p in products:
                try:
                    bien = _parse_product(p)
                except Exception:
                    continue
                if not bien:
                    continue
                cp = bien.get("code_postal") or ""
                if cp[:2] not in cibles:   # garde-fou département STRICT
                    continue
                bien["departement"] = cp[:2]
                if not keep_bien(bien, None, seen_ids, prix_max=prix_max,
                                 prix_min=prix_min, surface_min=surface_min):
                    continue
                biens.append(bien)

    print(f"[SenonchesImmo] {len(biens)} annonces")
    return biens


def _parse_product(p: dict) -> dict | None:
    titre = (p.get("title") or "").strip()
    if _EXCLUDE_STATUS.search(_fold(titre)):
        return None
    type_bien = (p.get("product_type") or "").strip().lower()
    if type_bien not in _KEEP_TYPES:
        return None

    variants = p.get("variants") or [{}]
    try:
        prix = float(variants[0].get("price") or 0) or None
    except (TypeError, ValueError):
        prix = None

    description = _strip_html(p.get("body_html") or "")

    # CP : « ... à Manou (28240) » dans le titre, sinon 1er CP plausible du corps
    m = re.search(r"\((\d{5})\)", titre)
    code_postal = m.group(1) if m else ""
    if not code_postal:
        m = re.search(r"\b(\d{5})\b", description)
        code_postal = m.group(1) if m else ""
    if not code_postal:
        return None

    # Ville : tag Shopify (commune), sinon « à vendre à X » du titre
    tags = p.get("tags") or []
    ville = (tags[0] if tags else "").strip()
    if not ville:
        m = re.search(r"à\s+([A-ZÉÈ][\w'’-]+(?:[ -][A-ZÉÈa-z][\w'’-]+)*)\s*\(\d{5}\)",
                      titre)
        ville = m.group(1).strip() if m else ""

    surface = _surface_habitable(titre) or _surface_habitable(description)

    surface_terrain = None
    m = re.search(r"terrain[^0-9]{0,40}(\d[\d\s\xa0]{2,8})\s*m[²2]", description,
                  re.IGNORECASE)
    if m:
        try:
            surface_terrain = float(re.sub(r"[\s\xa0]", "", m.group(1)))
        except ValueError:
            pass

    pieces = parse_int(r"(\d+)\s*pi[eè]ces", titre) or parse_int(
        r"(\d+)\s*pi[eè]ces", description)
    chambres = parse_int(r"(\d+)\s*chambres", titre) or parse_int(
        r"(\d+)\s*chambres", description)
    dpe = None
    m = re.search(r"DPE\s*:?\s*([A-G])\b", description, re.IGNORECASE)
    if m:
        dpe = m.group(1).upper()

    photos = [img.get("src") for img in (p.get("images") or []) if img.get("src")][:10]

    handle = p.get("handle") or ""
    return {
        "source": "senonches_immobilier",
        "url": f"{BASE_URL}/products/{handle}",
        "id_annonce": str(p.get("id") or handle),
        "titre": titre[:150],
        "type_bien": type_bien or "maison",
        "description": description[:1200],
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Senonches Immobilier",
    }


if __name__ == "__main__":
    standalone_main(search, "Senonches Immobilier")

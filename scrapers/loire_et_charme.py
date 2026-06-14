"""scrapers/loire_et_charme.py — Loire et Charme (Blois / Vendôme, Loir-et-Cher 41)

Méthode : scrape_simple (httpx) — SSR HTML (Drupal, données Poliris).
Agence spécialisée biens de caractère au nord de Blois vers Vendôme (41), proche
gare TGV ; déborde possiblement sur les communes limitrophes (37/45/28 cibles).

URL liste : /recherche-de-bien-immobilier   (toutes les annonces sur une page, ~78)
Cartes : article.node--type-bien-immobilier
  - titre/ville : h2 (ou .field--name-title)  « BLOIS - Vienne » / « VINEUIL, les noëls »
                  → la VILLE est le segment avant « - » ou « , » (PAS de code postal)
  - type        : segment d'URL /nos-biens-a-la-vente/{type}/...
  - réf         : .field--name-field-reference
  - prix        : .field--name-field-prix-de-vente-hai   « 174.000€ »
  - surface     : .field--name-field-surface             « 80m2 »
  - terrain     : .field--name-field-terrain             « 90m2 »
  - chambres    : .field--name-field-nombre-de-chambres  « 2 chambre(s) »
  - URL         : a.b-link

On exclut les appartements (on garde maison/demeure/fermette/longère/château).
Filtre département : aucun code postal sur la carte → on résout le NOM DE COMMUNE
(extrait du titre) en (dept, CP) via geo.api.gouv.fr (scrapers/_geo_resolve.py),
puis POST-FILTRE STRICT code_postal[:2] ∈ départements cibles → 0 fuite.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_int, standalone_main
from scrapers._geo_resolve import resolve_dept

BASE_URL = "https://www.loireetcharme.com"
LIST_URL = BASE_URL + "/recherche-de-bien-immobilier"
PHOTOS_PER_CARD = 3

_EXCLUDE_TYPE = ("appartements",)
_TYPE_LABEL = {
    "demeures-de-caractere": "demeure",
    "maisons-anciennes": "maison",
    "fermettes-et-longeres": "longere",
    "maisons-contemporaines": "maison",
    "chateaux-et-manoirs": "manoir",
}


# Titres possibles : « BLOIS, Basilique », « Sud de BLOIS », « Proche de VENDOME »,
# « MUIDES-SUR-LOIRE », « VINEUIL - Centre ». La COMMUNE est le plus long token en
# capitales (éventuellement avec traits d'union/apostrophes).
_CITY_TOKEN = re.compile(r"[A-ZÉÈÀÂÊÎÔÛÇ][A-ZÉÈÀÂÊÎÔÛÇ'’\-]{2,}(?:[ -][A-ZÉÈÀÂÊÎÔÛÇ'’\-]{2,})*")
_STATUS = re.compile(r"sous\s+(offre|compromis)|vendu|r[ée]serv", re.I)


def _city_from_title(title: str) -> str:
    if not title or _STATUS.search(title):
        return ""
    tokens = _CITY_TOKEN.findall(title)
    if not tokens:
        return ""
    # le token le plus long (souvent la commune, pas « SUD »/« GARE »)
    return max(tokens, key=len).strip()


def _parse_price_fr(text: str) -> float | None:
    # « 174.000€ » : le point est un séparateur de milliers (pas décimal).
    cleaned = re.sub(r"[^\d]", "", text or "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _num(text: str) -> float | None:
    m = re.search(r"([\d\s\xa0.]+)\s*m", text or "")
    if not m:
        return None
    val = re.sub(r"[\s\xa0.]", "", m.group(1))
    try:
        return float(val) if val else None
    except ValueError:
        return None


def _parse_card(card) -> dict | None:
    a = card.select_one("a.b-link")
    href = a.get("href") if a else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href
    seg = ""
    if "/nos-biens-a-la-vente/" in url:
        seg = url.split("/nos-biens-a-la-vente/")[1].split("/")[0]
    if seg in _EXCLUDE_TYPE:
        return None
    type_bien = _TYPE_LABEL.get(seg, "maison")

    title_el = card.select_one("h2, .field--name-title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    ville = _city_from_title(titre)

    def field(name: str) -> str:
        el = card.select_one(f".field--name-{name}")
        return el.get_text(" ", strip=True) if el else ""

    ref = field("field-reference")
    prix = _parse_price_fr(field("field-prix-de-vente-hai"))
    surface = _num(field("field-surface"))
    terrain = _num(field("field-terrain"))
    chambres = parse_int(r"(\d+)\s*chambre", field("field-nombre-de-chambres"))
    pieces = parse_int(r"(\d+)\s*pi[èe]ce", field("field-nombre-de-pieces"))

    id_annonce = ref or url

    photos = []
    for img in card.select("img[data-src]"):
        src = img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(src if src.startswith("http") else BASE_URL + src)
        if len(photos) >= PHOTOS_PER_CARD:
            break

    return {
        "source": "loire_et_charme",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": "",
        "ville": ville[:80],
        "code_postal": "",
        "surface": surface,
        "surface_terrain": terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Loire et Charme",
    }


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen: set[str] = set()
    geo_cache: dict[str, tuple[str, str]] = {}

    async with make_client() as client:
        r = await get_with_retry(client, LIST_URL)
        if r is None or r.status_code != 200:
            print(f"[LoireEtCharme] Liste inaccessible (status {getattr(r, 'status_code', '?')})")
            return results
        cards = BeautifulSoup(r.text, "html.parser").select("article.node--type-bien-immobilier")

        kept: dict[str, int] = {}
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien or not bien.get("ville"):
                continue
            dept, cp = await resolve_dept(client, bien["ville"], geo_cache)
            if not cp or cp[:2] not in departements:
                continue
            bien["code_postal"] = cp
            bien["departement"] = dept or cp[:2]
            aid = bien.get("id_annonce")
            if aid in seen:
                continue
            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue
            seen.add(aid)
            results.append(bien)
            kept[cp[:2]] = kept.get(cp[:2], 0) + 1
            await asyncio.sleep(0.05)

    print(f"[LoireEtCharme] {len(cards)} cartes → {len(results)} retenues par dept {kept}")
    return results


if __name__ == "__main__":
    standalone_main(search, "Loire et Charme")

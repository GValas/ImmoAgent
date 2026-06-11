"""scrapers/advicim.py — ADVICIM (réseau de mandataires immobiliers indépendants)

Réseau de conseillers indépendants né en Centre-Val de Loire (2020), ouvert au
national depuis 2022. Portail propre propulsé par Netty.immo.

Méthode : scrape_simple (httpx) — SSR HTML (ISO-8859-1).
URL pattern : /annonces/transaction/vente.html?manufacturers_id=transaction&page=N
              → liste NATIONALE (tous départements mélangés), 18 cartes/page,
                ~25 pages. PAS de filtre département serveur fiable (la recherche
                avancée Netty est un POST à champs cryptiques C_NN) → on pagine
                la liste complète et on POST-FILTRE STRICTEMENT sur le code
                département lu dans la localisation de chaque carte.

Cartes : div.product--card-listing (dupliquées mobile/desktop → dédup par id fiche)
  - URL/id : a.product--card__cover-link[href] → ../fiches/{cat}_{ID}/slug.html
  - Réf    : .product--card__content__reference        → "Réf. 2535-MAXIMO"
  - Loc    : .product--card__content__location         → "Charenton-du-Cher (18)"
             (⚠ département seul entre parenthèses, PAS de code postal complet)
  - Titre  : .product--card__content__title a          → "Maison à vendre …"
  - Pictos : .product--card__content__pictos__item--bedroom  → chambres
             .product--card__content__pictos__item--surface  → "170 m²"
             .product--card__content__pictos__item--room      → pièces
  - Prix   : .product--card__content__price            → "87 000 €"
  - Photos : .product--card__content__img img[src]

Type de bien : déduit du titre/slug (on ne garde que maisons / propriétés /
               longères / fermes / manoirs… ; on exclut appartement/terrain/
               immeuble/local/garage/parking).

Filtre département : STRICT sur le code à 2 chiffres entre parenthèses de la
                     localisation (== aux départements cibles). Comme la carte
                     ne donne pas le code postal complet, `code_postal` reste
                     vide et `departement` porte le filtre — 0 fuite vérifiée.

Politesse : 202/0-carte = soft-rate-limit du serveur → on espace les requêtes
            (asyncio.sleep ~1 s) et on retente une fois en cas de page vide.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.advicim.com"
LIST_URL = BASE_URL + "/annonces/transaction/vente.html"
MAX_PAGES = 30
PHOTOS_PER_CARD = 8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien à conserver (titre/slug) — maisons, propriétés, longères…
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|longere|long[èe]re|manoir|chateau|"
    r"ch[âa]teau|moulin|demeure|domaine|mas|gite|g[îi]te|corps[- ]de[- ]ferme|"
    r"maison de village|fermette|grange|p[âa]villon|pavillon|bastide",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"hangar|entrep[ôo]t|cave|box",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            cards = await _fetch_page(client, page)
            if cards is None:
                # erreur réseau dure → on arrête proprement
                break
            if not cards:
                # page sans carte (fin de liste ou rate-limit persistant) → stop
                break

            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # FILTRE DÉPARTEMENT STRICT (le code dept vient de "Ville (NN)")
                if bien["departement"] not in departements:
                    continue

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
                results.append(bien)
                new_on_page += 1

            print(f"[Advicim] Page {page}: {len(cards)} cartes, {new_on_page} retenues (zone)")
            await asyncio.sleep(1.0)

    # Récap par département (utile au debug / vérification fuite)
    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    if by_dept:
        print(f"[Advicim] Total zone: {len(results)} — détail {by_dept}")
    return results


async def _fetch_page(client: httpx.AsyncClient, page: int):
    """Retourne la liste des cartes dédupliquées, [] si vide, None si erreur dure."""
    url = f"{LIST_URL}?manufacturers_id=transaction&page={page}"
    for attempt in range(2):
        try:
            r = await client.get(url)
        except Exception as e:
            print(f"[Advicim] Erreur réseau page {page}: {e}")
            return None
        # Le serveur Netty renvoie parfois 202 (soft rate-limit) avec un corps vide
        if r.status_code == 200 and r.text:
            soup = BeautifulSoup(r.text, "html.parser")
            cards = _dedup_cards(soup)
            if cards:
                return cards
        if attempt == 0:
            await asyncio.sleep(2.0)  # on laisse retomber le rate-limit puis on retente
    return []


def _dedup_cards(soup) -> list:
    """Les cartes sont dupliquées (mobile/desktop). On dédoublonne par id fiche."""
    out = []
    seen = set()
    for card in soup.select("div.product--card-listing"):
        link = card.select_one("a.product--card__cover-link") or card.select_one(
            ".product--card__content__title a"
        )
        href = link.get("href", "") if link else ""
        fid = _fiche_id(href)
        if not fid or fid in seen:
            continue
        seen.add(fid)
        out.append(card)
    return out


def _parse_card(card) -> dict | None:
    link = card.select_one("a.product--card__cover-link") or card.select_one(
        ".product--card__content__title a"
    )
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = _abs_url(href)
    fid = _fiche_id(href)

    # Localisation : "Charenton-du-Cher (18)"
    loc_el = card.select_one(".product--card__content__location")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, dept = _parse_loc(loc)
    if not dept:
        return None

    # Titre
    title_el = card.select_one(".product--card__content__title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"Maison à vendre {ville}".strip()

    # Type de bien (titre + slug d'URL)
    type_src = f"{titre} {href}"
    if _EXCLUDE_TYPE.search(type_src) and not _KEEP_TYPE.search(type_src):
        return None
    if not _KEEP_TYPE.search(type_src):
        return None
    type_bien = _guess_type(type_src)

    # Référence (id_annonce de secours)
    ref_el = card.select_one(".product--card__content__reference")
    ref = ref_el.get_text(strip=True) if ref_el else ""
    ref = re.sub(r"(?i)^r[ée]f\.?\s*", "", ref).strip()
    id_annonce = fid or ref or url

    # Prix
    price_el = card.select_one(".product--card__content__price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Pictos : chambres / surface / pièces
    chambres = _picto_int(card, "bedroom")
    pieces = _picto_int(card, "room")
    surface = _picto_surface(card)

    # Photos
    photos = []
    for img in card.select(".product--card__img img, .product--card-listing__img img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            photos.append(_abs_url(src))
    # dédup en conservant l'ordre
    photos = list(dict.fromkeys(photos))[:PHOTOS_PER_CARD]

    return {
        "source": "advicim",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",  # la carte ne donne que le dept (NN), pas le CP complet
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Advicim",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _abs_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    # liens relatifs type ../fiches/... ou ../office20/...
    cleaned = href.lstrip("./")
    return f"{BASE_URL}/{cleaned}"


def _fiche_id(href: str) -> str:
    """../fiches/4-40-26_60763164/slug.html → '60763164'."""
    m = re.search(r"/fiches/[^/]*_(\d+)/", href)
    return m.group(1) if m else ""


def _parse_loc(text: str) -> tuple[str, str]:
    """'Charenton-du-Cher (18)' → ('Charenton-du-Cher', '18')."""
    dept = ""
    m = re.search(r"\((\d{2,3})\)", text)
    if m:
        dept = m.group(1)[:2]
    ville = re.sub(r"\s*\(\d{2,3}\)\s*$", "", text).strip()
    return ville, dept


def _parse_price(text: str) -> float | None:
    # "87 000 €" (espaces fines insécables, &#8239;)
    cleaned = re.sub(r"[^\d]", "", text.replace(" ", "").replace("\xa0", ""))
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    # garde-fou : un prix immobilier plausible
    if v is not None and v < 1000:
        return None
    return v


def _picto_int(card, suffix: str) -> int | None:
    el = card.select_one(f".product--card__content__pictos__item--{suffix}")
    if not el:
        return None
    m = re.search(r"(\d+)", el.get_text(" ", strip=True))
    return int(m.group(1)) if m else None


def _picto_surface(card) -> float | None:
    el = card.select_one(".product--card__content__pictos__item--surface")
    if not el:
        return None
    m = re.search(r"([\d\s\xa0 ]+)\s*m", el.get_text(" ", strip=True))
    if m:
        val = re.sub(r"[\s\xa0 ]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 3000:
                return f
        except ValueError:
            pass
    return None


def _guess_type(text: str) -> str:
    t = text.lower()
    for kw in (
        "longère", "longere", "manoir", "château", "chateau", "moulin",
        "demeure", "domaine", "ferme", "fermette", "grange", "propriété",
        "propriete", "mas", "bastide", "villa", "pavillon",
    ):
        if kw in t:
            return kw.replace("longere", "longère").replace("propriete", "propriété")
    return "maison"


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
    print(f"\nTotal Advicim: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p/{b.get('chambres') or '?'}ch"
            f" — {b['type_bien']} — {b['ville']}"
        )

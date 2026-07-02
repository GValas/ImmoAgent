"""scrapers/av_transaction.py — AV Transaction (réseau national d'agences)

Méthode : scrape_simple (httpx) — SSR HTML + JSON-LD
Site     : https://av-transaction.immo  (Next.js, mais rendu SSR exploitable)

URL pattern : /acheter/{region-slug}-{NN}-r/?page={N}
  Le code {NN} est le code INSEE de la RÉGION (pas du département) :
    24 = Centre-Val de Loire, 27 = Bourgogne-Franche-Comté,
    52 = Pays de la Loire, 28 = Normandie, 32 = Hauts-de-France...
  → Il n'existe AUCUN filtre par département côté serveur, seulement par région.
    On scrape donc la région contenant le département cible, puis on POST-FILTRE
    strictement sur code_postal[:2] == dept (0 fuite vérifié).

Données : 12 annonces / page, présentes à la fois :
  - en JSON-LD <script type="application/ld+json"> @type=OfferForPurchase
    (name, price, priceCurrency, category, serialNumber=id, description, image[])
  - en cartes HTML : div.group  (1 lien /annonce/{serialNumber}/ par carte)
    → titre + "Ville ( CODEPOSTAL )" + prix dans le texte de la carte.
  La ville/CP n'étant PAS fiable dans le JSON-LD, on les lit sur la carte HTML
  et on enrichit le reste (prix/type/surface/pièces/photos) depuis le JSON-LD.

URL détail : /annonce/{serialNumber}/

Type de bien : champ `category` du JSON-LD (Maison, Appartement, Grange,
  Corps de ferme, Propriété forestière, Locaux d'activité...). On ne garde que
  maisons / propriétés / biens d'habitation ruraux ; on exclut appartements,
  locaux, terrains nus, etc.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://av-transaction.immo"
# Le site (Next.js) THROTTLE agressivement le route /acheter/ : après quelques
# requêtes rapprochées il renvoie une page SSR VIDE (~52 Ko, 0 annonce, status 200)
# pendant ~5 min. La pagination ?page=N est donc impraticable sans proxy rotatif.
# Stratégie retenue : 1 SEULE requête par région (URL nue = page 1, 12 annonces),
# délai généreux entre régions, 1 retry après pause si la page revient vide.
# → on récupère les 12 annonces les plus récentes par région, puis post-filtre dept.
MAX_PAGES = 1
PHOTOS_PER_CARD = 10
PAGE_DELAY = 3.0
REGION_DELAY = 5.0
RETRY_DELAY = 8.0


# Code département → slug de RÉGION administrative (code INSEE région inclus).
# Plusieurs départements partagent la même URL régionale : on dédoublonne au scrape.
DEPT_REGIONS: dict[str, str] = {
    # Centre-Val de Loire (24)
    "28": "centre-val-de-loire-24-r",
    "45": "centre-val-de-loire-24-r",
    "37": "centre-val-de-loire-24-r",
    "36": "centre-val-de-loire-24-r",
    "18": "centre-val-de-loire-24-r",
    "41": "centre-val-de-loire-24-r",
    # Pays de la Loire (52)
    "72": "pays-de-la-loire-52-r",
    "49": "pays-de-la-loire-52-r",
    "53": "pays-de-la-loire-52-r",
    # Bourgogne-Franche-Comté (27)
    "89": "bourgogne-franche-comte-27-r",
    "58": "bourgogne-franche-comte-27-r",
}

# Types de bien (category JSON-LD) à conserver : maisons / propriétés / rural.
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|grange|corps de ferme|"
    r"ch[âa]let|b[âa]tisse|maison de village|maison de ville|fermette",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|locaux|commerce|garage|parking|immeuble|"
    r"bureau|fonds|studio|forest|bois|investissement|viager",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Regroupe les départements cibles par région (1 seul scrape par région).
    region_to_depts: dict[str, list[str]] = {}
    for dept in departements:
        region = DEPT_REGIONS.get(dept)
        if not region:
            continue
        region_to_depts.setdefault(region, []).append(dept)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for region, depts in region_to_depts.items():
            try:
                biens = await _scrape_region(
                    client, region, depts, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                par_dept = {
                    d: sum(1 for b in biens if b["departement"] == d) for d in depts
                }
                print(f"[AVTransaction] Région {region}: {len(biens)} annonces {par_dept}")
            except Exception as e:
                print(f"[AVTransaction] Erreur région {region}: {e}")
            await asyncio.sleep(REGION_DELAY)

    return results


async def _fetch_cards(client: httpx.AsyncClient, url: str):
    """GET une page ; si SSR vide (throttle), pause + 1 retry. Retourne (soup, cards)."""
    r = await client.get(url)
    if r.status_code != 200:
        return None, []
    soup = BeautifulSoup(r.text, "html.parser")
    cards = _iter_cards(soup)
    if not cards:
        # Page potentiellement vidée par le throttle → on laisse respirer et on retente.
        await asyncio.sleep(RETRY_DELAY)
        r = await client.get(url)
        if r.status_code != 200:
            return None, []
        soup = BeautifulSoup(r.text, "html.parser")
        cards = _iter_cards(soup)
    return soup, cards


async def _scrape_region(
    client: httpx.AsyncClient,
    region: str,
    depts: list[str],
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()
    seen_hrefs: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        # Page 1 = URL nue (le ?page=1 peut différer côté SSR Next.js).
        if page == 1:
            url = f"{BASE_URL}/acheter/{region}/"
        else:
            url = f"{BASE_URL}/acheter/{region}/?page={page}"

        soup, cards = await _fetch_cards(client, url)
        if not cards:
            break

        # Détecte la fin de pagination : aucune nouvelle annonce → on arrête.
        page_hrefs = {h for _, h in cards}
        if page_hrefs and page_hrefs <= seen_hrefs:
            break
        seen_hrefs |= page_hrefs

        ld_by_id = _index_jsonld(soup)

        for card, href in cards:
            try:
                bien = _parse_card(card, href, ld_by_id, depts)
            except Exception:
                continue
            if not bien:
                continue

            # Post-filtre département STRICT (0 fuite hors-zone).
            if not bien["code_postal"] or bien["code_postal"][:2] not in depts:
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
            biens.append(bien)

        await asyncio.sleep(PAGE_DELAY)

    return biens


def _index_jsonld(soup) -> dict[str, dict]:
    """serialNumber → dict OfferForPurchase."""
    out: dict[str, dict] = {}
    for s in soup.find_all("script", type="application/ld+json"):
        raw = s.string or s.get_text() or ""
        if "OfferForPurchase" not in raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("@type") == "OfferForPurchase":
            sn = str(d.get("serialNumber") or "")
            if sn:
                out[sn] = d
    return out


def _iter_cards(soup) -> list[tuple]:
    """Retourne [(card_div, href), ...] dédupliqués par href /annonce/{id}/."""
    cards: list[tuple] = []
    seen: set[str] = set()
    for a in soup.select('a[href^="/annonce/"]'):
        href = a.get("href", "")
        if not href or href in seen:
            continue
        seen.add(href)
        card = a.find_parent("div", class_="group")
        if card is None:
            card = a.parent
        cards.append((card, href))
    return cards


def _parse_card(card, href: str, ld_by_id: dict, depts: list[str]) -> dict | None:
    m_id = re.search(r"/annonce/(\d+)", href)
    serial = m_id.group(1) if m_id else ""
    url = BASE_URL + href if href.startswith("/") else href

    ld = ld_by_id.get(serial, {})

    # Type de bien : category JSON-LD en priorité, sinon titre.
    category = (ld.get("category") or "").strip()
    name = (ld.get("name") or "").strip()
    type_src = category or name
    # EXCLUDE prime sur KEEP (ex. "Propriété forestière" = terrain/bois, pas habitation).
    if _EXCLUDE_TYPE.search(type_src):
        return None
    if not _KEEP_TYPE.search(type_src):
        return None
    type_bien = (category or name.split(" à vendre")[0]).strip().lower() or "maison"

    # Ville + code postal depuis le texte de la carte : "Ville ( 45650 )"
    card_text = card.get_text(" ", strip=True)
    ville, code_postal = _parse_loc(card_text)

    # Titre
    titre = name or f"{type_bien.title()} {ville}".strip()

    # Description
    description = (ld.get("description") or "").strip()

    # Prix : JSON-LD, sinon texte de la carte.
    prix = ld.get("price")
    try:
        prix = float(prix) if prix not in (None, "") else None
    except (TypeError, ValueError):
        prix = None
    if prix is None:
        prix = _parse_price_from_text(card_text)

    # Surface habitable + pièces depuis le name "... - N pièce(s) - NNNm2"
    surface = _parse_surface(name) or _parse_surface(description)
    pieces = _parse_pieces(name)

    # Photos depuis le JSON-LD (image peut être str ou list).
    photos = _parse_images(ld.get("image"))
    if not photos:
        for img in card.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if src.startswith("http") and "avtransaction" in src:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    dept = code_postal[:2] if code_postal else ""

    return {
        "source": "av_transaction",
        "url": url,
        "id_annonce": serial or url,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "AV Transaction",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'... Saint-jean-le-blanc ( 45650 ) ...' → ('Saint-jean-le-blanc', '45650')"""
    m = re.search(r"([A-Za-zÀ-ÿ'’\- ]+?)\s*\(\s*(\d{5})\s*\)", text)
    if not m:
        return "", ""
    ville = re.sub(r"\s+", " ", m.group(1)).strip(" -")
    return ville, m.group(2)


def _parse_price_from_text(text: str) -> float | None:
    m = re.search(r"([\d\s\xa0]{3,})\s*€", text)
    if not m:
        return None
    cleaned = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'Maison à vendre - 6 pièce(s) - 120m2' → 120.0"""
    if not text:
        return None
    m = re.search(r"([\d]+(?:[.,]\d+)?)\s*m2\b", text, re.IGNORECASE)
    if not m:
        return None
    try:
        f = float(m.group(1).replace(",", "."))
        if 5 <= f <= 5000:
            return f
    except ValueError:
        pass
    return None


def _parse_pieces(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d+)\s*pi[eè]ce", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_images(image) -> list[str]:
    if not image:
        return []
    if isinstance(image, str):
        return [image] if image.startswith("http") else []
    if isinstance(image, list):
        return [u for u in image if isinstance(u, str) and u.startswith("http")]
    return []


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
    print(f"\nTotal AV Transaction: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

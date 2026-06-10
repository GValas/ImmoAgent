"""scrapers/coteimmobilier.py — Cote Immobilier (agence locale, Angers 49)

Méthode : scrape_simple (httpx) — SSR HTML (plateforme Orisha / realestate.orisha.com)
Agence mono-implantation à Angers (49) depuis 1998 → quasi tout le stock est en
Maine-et-Loire, mais on POST-FILTRE STRICTEMENT sur code_postal[:2] (0 fuite).

URL liste : /annonces/transaction/Vente.html?page=N
  Cartes : div.item-product
    - URL détail : .visuel-product a[href]  (→ /fiches/...)  ou .products-link parent
    - Titre      : .products-name
    - Description : .products-desc
    - Réf        : .products-ref           → "Ref. : 4228"
    - Prix       : .products-price          → "306 000 € dont 5.52% TTC d'honoraires"
    - Photo      : .visuel-product img.photo[src]
  Le code postal / ville N'EST PAS dans la carte → on visite la page détail.

URL détail : /fiches/{slug}.html
  Champs structurés en ul.list-group → li (label | valeur) :
    Code postal, Ville, Nombre pièces, Chambres, Consommation énergie finale (DPE).
  Surface habitable : pas dans la liste → extraite du titre ("T4 d'environ 115,57 m²").

Filtre département : POST-FILTRE strict code_postal[:2] ∈ departements ciblés.
Volume : agence locale, ~17 annonces tous types confondus (vérifié 2026-06-10).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.coteimmobilier.fr"
LIST_URL = BASE_URL + "/annonces/transaction/Vente.html"
MAX_PAGES = 10
PHOTOS_PER_CARD = 1  # la liste n'expose qu'une vignette ; galerie enrichie ailleurs

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien à conserver : maisons / propriétés / fermes...
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|long[èe]re|manoir|ch[âa]teau|moulin|"
    r"demeure|domaine|mas|g[îi]te|corps[- ]de[- ]ferme|maison de village",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|terrain|local|commerc|garage|parking|immeuble|bureau|"
    r"fonds|cave|box",
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
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1) Collecte des cartes (toutes pages)
        cards_data = await _collect_cards(client)
        print(f"[CoteImmobilier] {len(cards_data)} annonces listées")

        # 2) Enrichissement page détail (CP/ville/pièces/DPE) + post-filtre dept
        for card in cards_data:
            aid = card["id_annonce"]
            if aid in seen_ids:
                continue
            try:
                bien = await _enrich_detail(client, card)
            except Exception as e:
                print(f"[CoteImmobilier] Erreur détail {card.get('url')}: {e}")
                continue
            await asyncio.sleep(0.5)
            if not bien:
                continue

            cp = bien.get("code_postal") or ""
            if not cp or cp[:2] not in departements:
                continue  # POST-FILTRE STRICT : 0 fuite hors-zone

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

    # Récap départements
    vus = sorted({b["code_postal"][:2] for b in results if b["code_postal"]})
    print(f"[CoteImmobilier] {len(results)} retenus — départements {vus}")
    return results


async def _collect_cards(client: httpx.AsyncClient) -> list[dict]:
    cards: list[dict] = []
    seen_urls: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        params = {"manufacturers_id": "transaction", "page": page}
        r = await client.get(LIST_URL, params=params)
        if r.status_code != 200:
            break

        items = BeautifulSoup(r.text, "html.parser").select("div.item-product")
        if not items:
            break

        new_on_page = 0
        for it in items:
            data = _parse_card(it)
            if not data:
                continue
            if data["url"] in seen_urls:
                continue
            seen_urls.add(data["url"])
            cards.append(data)
            new_on_page += 1

        if new_on_page == 0:
            break
        await asyncio.sleep(0.5)

    return cards


def _parse_card(card) -> dict | None:
    link = card.select_one(".visuel-product a[href]") or card.select_one(
        "a.products-link[href]"
    )
    if not link:
        # fallback : tout lien vers /fiches/
        link = card.find("a", href=re.compile(r"/fiches/"))
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = _abs_url(href)

    name_el = card.select_one(".products-name")
    titre = name_el.get_text(" ", strip=True) if name_el else ""
    if not titre:
        return None

    # Filtre type (sur le titre) : on ne garde que maisons / propriétés
    if _EXCLUDE_TYPE.search(titre) and not _KEEP_TYPE.search(titre):
        return None
    if not _KEEP_TYPE.search(titre):
        return None

    desc_el = card.select_one(".products-desc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    ref_el = card.select_one(".products-ref")
    ref_txt = ref_el.get_text(" ", strip=True) if ref_el else ""
    m_ref = re.search(r"Ref\.?\s*:?\s*([\w\-]+)", ref_txt)
    ref = m_ref.group(1) if m_ref else ""

    price_el = card.select_one(".products-price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # id annonce : data-productid sur le bouton favori, sinon ref, sinon url
    fav = card.select_one("[data-productid]")
    pid = fav.get("data-productid") if fav else ""
    id_annonce = pid or ref or url

    photos = []
    img = card.select_one(".visuel-product img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(_abs_url(src))

    surface = _parse_surface_hab(titre)

    return {
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "description": description[:1200],
        "prix": prix,
        "surface": surface,
        "photos": photos[:PHOTOS_PER_CARD],
        "ref": ref,
    }


async def _enrich_detail(client: httpx.AsyncClient, card: dict) -> dict | None:
    r = await client.get(card["url"])
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    # Dictionnaire label → valeur depuis les ul.list-group
    fields: dict[str, str] = {}
    for ul in soup.select("ul.list-group"):
        for li in ul.find_all("li", recursive=False):
            parts = list(li.stripped_strings)
            if len(parts) >= 2:
                fields[parts[0].strip()] = " ".join(parts[1:]).strip()

    code_postal = ""
    cp_raw = fields.get("Code postal", "")
    m_cp = re.search(r"\b(\d{5})\b", cp_raw)
    if m_cp:
        code_postal = m_cp.group(1)
    ville = fields.get("Ville", "").title()

    type_bien = (fields.get("Type de bien") or "maison").strip().lower()

    # Filtre type définitif (le titre liste peut être trompeur) : on rejette
    # appartements, parkings, terrains, commerces… sur le type réel de la fiche.
    if type_bien and _EXCLUDE_TYPE.search(type_bien) and not _KEEP_TYPE.search(
        type_bien
    ):
        return None

    pieces = _to_int(fields.get("Nombre pièces"))
    chambres = _to_int(fields.get("Chambres"))

    dpe = None
    dpe_raw = fields.get("Consommation énergie finale", "")
    m_dpe = re.match(r"\s*([A-G])\b", dpe_raw)
    if m_dpe:
        dpe = m_dpe.group(1)

    surface_terrain = None
    for k, v in fields.items():
        if re.search(r"terrain", k, re.IGNORECASE):
            m = re.search(r"([\d\s\xa0]+)", v)
            if m:
                try:
                    surface_terrain = float(re.sub(r"[\s\xa0]", "", m.group(1)))
                except ValueError:
                    pass

    surface = card.get("surface")
    if surface is None:
        # tentative depuis surface "habitable" éventuellement listée
        for k, v in fields.items():
            if re.search(r"surface|habitable", k, re.IGNORECASE):
                s = _parse_surface_hab(v)
                if s:
                    surface = s
                    break

    return {
        "source": "coteimmobilier",
        "url": card["url"],
        "id_annonce": card["id_annonce"],
        "titre": card["titre"],
        "type_bien": type_bien,
        "description": card["description"],
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": card.get("prix"),
        "photos": card.get("photos", []),
        "dpe": dpe,
        "agence": "Cote Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _abs_url(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    href = href.lstrip(".")  # "../fiches/..." → "/fiches/..."
    if not href.startswith("/"):
        href = "/" + href
    return BASE_URL + href


def _parse_price(text: str) -> float | None:
    # garde la partie avant "dont ..." pour éviter le % d'honoraires
    text = re.split(r"\bdont\b", text, flags=re.IGNORECASE)[0]
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _to_int(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


def _parse_surface_hab(text: str) -> float | None:
    """'T4 d'environ 115,57 m²' / '202,80 m² habitables' → float."""
    if not text:
        return None
    m = re.search(r"([\d]{1,4}(?:[.,]\d+)?)\s*m(?:²|2)\b", text)
    if m:
        val = m.group(1).replace(",", ".")
        try:
            f = float(val)
            if 8 <= f <= 3000:
                return f
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
    print(f"\nTotal Cote Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p — DPE {b['dpe'] or '?'}"
            f" — {b['type_bien']} — {b['ville']}"
        )

"""scrapers/immoa2.py — IMMOA2 (agence locale, Pithiviers / Loiret 45)

Méthode : scrape_simple (httpx) — SSR HTML (CMS Immo-Facile, v2.immo-facile.com).
URL liste : /annonces/transaction/Vente.html             (page 1)
            /annonces/transaction_____{N}/vente.html      (pages suivantes)
            → catalogue national de l'agence ; PAS de filtre département serveur
              fiable. On scrape tout, puis POST-FILTRE strict sur le code postal
              récupéré en page détail (CP[:2] ∈ départements cibles).

La pagination « clampe » sur la dernière page (la page N+1 répète la dernière) :
on déduplique par href et on s'arrête dès qu'une page n'apporte plus rien.

Cartes (vue liste) : div.product
  - URL    : a.product-image[href]  → /fiches/{TYPECODE}_{ID}/{slug}.html
             TYPECODE : 4-40-26=maison, 3-33-*=appartement, 11-*=terrain, 8_=immeuble
  - Titre  : .product-name           → "Maison Nibelle 4 pièce(s) 96 m2 , Nibelle"
  - Prix   : .product-price          → "123 000 €"
  - Pièces : .data-list__item--NbPiece .data-list__item--value
  - Surface: .data-list__item--Surface .data-list__item--value
  - Réf    : .data-list__item--products_model .data-list__item--value
  - Photos : a.product-image img.photo[src] / img.photo-hidden[src]

Page détail (table li.list-group-item, col-sm-6 libellé / valeur) — fournit le
CP (indispensable au filtre dept), la ville, surface, terrain, pièces, chambres,
prix et DPE (« Consommation énergie primaire »).

Type de bien : on ne garde que maisons / propriétés / longères… (cf. _KEEP_TYPE),
on exclut appartements / terrains / immeubles / commerces.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.immoa2.com"
LISTE_P1 = "/annonces/transaction/Vente.html"
LISTE_PN = "/annonces/transaction_____{n}/vente.html"
MAX_PAGES = 12
PHOTOS_PER_CARD = 10

# Départements cibles (post-filtre strict sur CP[:2])
DEPTS_CIBLES = {"72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps de ferme|fermette|pavillon",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"loisir|hangar",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    # garde-fou : ne jamais sortir de la zone cible
    departements = departements & DEPTS_CIBLES if departements else DEPTS_CIBLES
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1) Collecte des cartes (toutes les pages, jusqu'au clamp)
        cartes: list[dict] = []
        seen_urls: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            path = LISTE_P1 if page == 1 else LISTE_PN.format(n=page)
            try:
                r = await client.get(BASE_URL + path)
            except Exception as e:
                print(f"[ImmoA2] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.product")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                c = _parse_card(card)
                if not c or c["url"] in seen_urls:
                    continue
                seen_urls.add(c["url"])
                cartes.append(c)
                new_on_page += 1

            print(f"[ImmoA2] Page {page}: {new_on_page} nouvelles cartes")
            if new_on_page == 0:  # clamp / fin de liste
                break
            await asyncio.sleep(0.5)

        # 2) Enrichissement détail + post-filtre dept (CP) sur les maisons/propriétés
        for c in cartes:
            try:
                bien = await _enrich_detail(client, c)
            except Exception as e:
                print(f"[ImmoA2] Erreur détail {c['url']}: {e}")
                continue
            if not bien:
                continue

            cp = bien["code_postal"]
            if not cp or cp[:2] not in departements:
                continue  # post-filtre STRICT — 0 fuite hors-zone

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            results.append(bien)
            await asyncio.sleep(0.4)

    print(f"[ImmoA2] {len(results)} biens retenus (zone cible)")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a.product-image") or card.select_one("a[href*='/fiches/']")
    href = link.get("href", "") if link else ""
    if not href or "/fiches/" not in href:
        return None
    url = _abs(href)

    title_el = card.select_one(".product-name")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    titre = re.sub(r"\s*,\s*$", "", titre).strip()

    # filtrage type (titre + segment d'URL)
    slug = href.rsplit("/", 1)[-1]
    if _EXCLUDE_TYPE.search(titre) and not _KEEP_TYPE.search(titre):
        return None
    if not _KEEP_TYPE.search(titre) and not _KEEP_TYPE.search(slug):
        return None

    price_el = card.select_one(".product-price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    pieces = _data_list_int(card, "NbPiece")
    surface = _data_list_float(card, "Surface")
    ref = _data_list_value(card, "products_model")

    m_id = re.search(r"_(\d+)/", href)
    id_annonce = ref or (m_id.group(1) if m_id else url)

    photos = []
    for img in card.select("a.product-image img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(_abs(src))
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "prix": prix,
        "pieces": pieces,
        "surface": surface,
        "photos": photos,
    }


async def _enrich_detail(client: httpx.AsyncClient, c: dict) -> dict | None:
    r = await client.get(c["url"])
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    champs = _detail_fields(soup)

    # Le libellé varie selon les fiches : « Code postal », « Code Postal Internet »…
    cp_raw = ""
    for k, v in champs.items():
        if k.startswith("code postal"):
            cp_raw = v
            break
    m_cp = re.search(r"\b(\d{5})\b", cp_raw)
    code_postal = m_cp.group(1) if m_cp else ""

    ville = (champs.get("ville") or "").strip().title()
    type_bien = (champs.get("type de bien") or "").strip().lower() or "maison"

    surface = _num(champs.get("surface")) or c.get("surface")
    surface_terrain = _num(champs.get("surface terrain"))
    pieces = _int(champs.get("nombre pieces") or champs.get("nombre pièces")) or c.get(
        "pieces"
    )
    chambres = _int(champs.get("chambres"))
    prix = _parse_price(champs.get("prix") or "") or c.get("prix")
    dpe = _parse_dpe(champs.get("consommation energie primaire")
                     or champs.get("consommation énergie primaire"))

    # description : bloc texte de l'annonce
    desc = ""
    desc_el = soup.select_one(".product-description, .description, #description")
    if desc_el:
        desc = desc_el.get_text(" ", strip=True)
    if not desc:
        og = soup.select_one("meta[property='og:description']")
        if og:
            desc = og.get("content", "")

    return {
        "source": "immoa2",
        "url": c["url"],
        "id_annonce": c["id_annonce"],
        "titre": c["titre"],
        "type_bien": type_bien,
        "description": desc[:1200],
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": c["photos"],
        "dpe": dpe,
        "agence": "IMMOA2",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _abs(href: str) -> str:
    if href.startswith("http"):
        return href
    return BASE_URL + "/" + href.lstrip("./").lstrip("/")


def _detail_fields(soup) -> dict:
    out: dict[str, str] = {}
    for li in soup.select("li.list-group-item"):
        cells = li.select(".col-sm-6")
        if len(cells) >= 2:
            k = cells[0].get_text(" ", strip=True).lower()
            v = cells[1].get_text(" ", strip=True)
            if k and k not in out:
                out[k] = v
    return out


def _data_list_value(card, suffix: str) -> str | None:
    el = card.select_one(
        f".data-list__item--{suffix} .data-list__item--value"
    )
    return el.get_text(strip=True) if el else None


def _data_list_int(card, suffix: str) -> int | None:
    v = _data_list_value(card, suffix)
    return _int(v)


def _data_list_float(card, suffix: str) -> float | None:
    v = _data_list_value(card, suffix)
    return _num(v)


def _parse_price(text: str) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"(?i)(eur|€|\s|\xa0)", "", text)
    cleaned = re.sub(r"[^\d.,]", "", cleaned).replace(",", ".")
    # gérer un éventuel séparateur décimal parasite
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _num(text) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", str(text).replace("\xa0", " "))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _int(text) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d+)", str(text))
    return int(m.group(1)) if m else None


def _parse_dpe(text) -> str | None:
    if not text:
        return None
    m = re.search(r"\b([A-G])\b", str(text).strip().upper())
    return m.group(1) if m else None


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
    print(f"\nTotal ImmoA2: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — DPE {b.get('dpe')}"
            f" — {b['type_bien']} — {b['ville']}"
        )

"""scrapers/privilegeimmo.py — Privilège Immo (Sens, Yonne)

Agence de Sens et environs (Yonne 89). Maisons de caractère, maisons de campagne,
propriétés avec dépendances. Bon vivier « caractère / rural ».

Méthode : scrape_simple (httpx) — SSR HTML (WordPress + plugin Apimo).
URL pattern :
  - Liste  : /nos-biens/      (cartes .apimo-properties-item, contenu dans le HTML)
  - Détail : /annonce/{slug}/

Filtre département : agence mono-zone, PAS de filtre serveur → POST-FILTRE strict
  sur code_postal[:2] ∈ départements cibles. 0 fuite. Le CP du bien est lisible
  directement sur la carte (.Pro-address "Villemanoche - 89140").

Cartes : div.apimo-properties-item
  - Type   : .Pro-category         →  "Maison"
  - Loc    : .Pro-address          →  "Villemanoche - 89140"  (ville + CP)
  - Métas  : .Pro-meta × N         →  "6 Pièces", "2 Salle de bain", "208 m²"
  - Prix   : .Pro-price            →  "€ 279.000 {ref}"
  - Réf    : .apimo-property-reference
  - URL    : a[href*=/annonce/]
  - Photos : img.single-apimo-img (wp-content/uploads/.../...-original.jpg)
Détail : h1 (titre + terrain), nb chambres, description, galerie complète.

Types : on garde maison / propriété / longère / ferme / manoir / château /
  moulin / domaine / demeure ; on exclut appartement / terrain / immeuble.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://privilegeimmo.fr"
LIST_URL = f"{BASE_URL}/nos-biens/"
PHOTOS_PER_BIEN = 10
CONCURRENCY = 5


_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|fermette|longere|longère|manoir|"
    r"chateau|château|moulin|demeure|domaine|gite|gîte|pavillon|corps de ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|immeuble|local|commerce|garage|parking|bureau|fonds",
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
            r = await client.get(LIST_URL)
            r.raise_for_status()
        except Exception as e:
            print(f"[PrivilegeImmo] Erreur liste: {e}")
            return results

        cards = BeautifulSoup(r.text, "html.parser").select(".apimo-properties-item")
        print(f"[PrivilegeImmo] {len(cards)} cartes sur /nos-biens")

        retained = []
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue
            cp = bien.get("code_postal") or ""
            if not cp or cp[:2] not in departements:
                continue
            tb = bien.get("type_bien") or ""
            if _EXCLUDE_TYPE.search(tb) and not _KEEP_TYPE.search(tb):
                continue
            if not _KEEP_TYPE.search(tb):
                continue
            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue
            retained.append(bien)

        print(f"[PrivilegeImmo] {len(retained)} cartes retenues (zone + type + "
              f"bornes) → enrichissement détail")

        sem = asyncio.Semaphore(CONCURRENCY)

        async def _enrich(bien: dict) -> dict:
            async with sem:
                try:
                    await _fill_detail(client, bien)
                except Exception as e:
                    print(f"[PrivilegeImmo] détail {bien['id_annonce']}: {e}")
                await asyncio.sleep(0.4)
                return bien

        retained = await asyncio.gather(*[_enrich(b) for b in retained])

    for bien in retained:
        cp = bien.get("code_postal") or ""
        if cp and cp[:2] in departements:
            results.append(bien)

    print(f"[PrivilegeImmo] {len(results)} biens retenus")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one('a[href*="/annonce/"]')
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    cat_el = card.select_one(".Pro-category")
    type_bien = cat_el.get_text(" ", strip=True).lower() if cat_el else "maison"

    addr_el = card.select_one(".Pro-address")
    addr = addr_el.get_text(" ", strip=True) if addr_el else ""
    ville, code_postal = _parse_addr(addr)

    ref_el = card.select_one(".apimo-property-reference")
    ref = ref_el.get_text(strip=True) if ref_el else ""
    if not ref:
        ref = href.rstrip("/").rsplit("/", 1)[-1]

    # métas : pièces / surface
    pieces = None
    surface = None
    for meta in card.select(".Pro-meta"):
        txt = meta.get_text(" ", strip=True)
        if "pièce" in txt.lower() or "piece" in txt.lower():
            m = re.search(r"(\d+)", txt)
            if m:
                pieces = int(m.group(1))
        elif "m²" in txt or re.search(r"\bm2\b", txt):
            m = re.search(r"([\d\s\xa0]+)\s*m", txt)
            if m:
                val = re.sub(r"[\s\xa0]", "", m.group(1))
                if val.isdigit() and 8 <= int(val) <= 2000:
                    surface = int(val)

    prix = None
    price_el = card.select_one(".Pro-price")
    if price_el:
        # retire la référence Apimo (souvent collée au prix) avant le parsing
        ref_in_price = price_el.select_one(".apimo-property-reference")
        if ref_in_price:
            ref_in_price.extract()
        prix = _parse_price(price_el.get_text(" ", strip=True))

    photos = []
    for img in card.select("img.single-apimo-img"):
        src = img.get("src") or img.get("data-src") or ""
        if src.startswith("http") and src not in photos:
            photos.append(src)
    photos = photos[:PHOTOS_PER_BIEN]

    return {
        "source": "privilegeimmo",
        "url": url,
        "id_annonce": ref,
        "titre": f"{type_bien.title()} {ville}".strip()[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2] if code_postal else None,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Privilège Immo",
    }


async def _fill_detail(client: httpx.AsyncClient, bien: dict) -> None:
    r = await client.get(bien["url"])
    if r.status_code != 200:
        return
    t = r.text
    soup = BeautifulSoup(t, "html.parser")

    h1 = soup.select_one("h1")
    if h1:
        bien["titre"] = h1.get_text(" ", strip=True)[:150]

    if bien.get("surface_terrain") is None:
        m = re.search(r"[Tt]errain[^0-9]{0,15}([\d\s\xa0]+)\s*m²", t)
        if m:
            val = re.sub(r"[\s\xa0]", "", m.group(1))
            if val.isdigit():
                bien["surface_terrain"] = float(val)

    if bien.get("chambres") is None:
        m = re.search(r"(\d+)\s*chambre", t, re.IGNORECASE)
        if m:
            bien["chambres"] = int(m.group(1))

    for sel in [".Pro-description", ".apimo-description", "[class*=description]",
                ".entry-content"]:
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 60:
            bien["description"] = el.get_text(" ", strip=True)[:1200]
            break

    photos = []
    for ph in re.findall(
        r"https://privilegeimmo\.fr/wp-content/uploads/\d+/\d+/[^ \"']+?-original\.jpg",
        t,
    ):
        if ph not in photos:
            photos.append(ph)
    if photos:
        bien["photos"] = photos[:PHOTOS_PER_BIEN]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_addr(text: str) -> tuple[str, str]:
    """'Villemanoche - 89140' → ('Villemanoche', '89140')"""
    cp = ""
    m = re.search(r"(\d{5})", text)
    if m:
        cp = m.group(1)
    ville = re.sub(r"[\-,]?\s*\d{5}.*$", "", text).strip(" -,").strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    """'€ 279.000 86975774' → 279000 (1er bloc après €, point = milliers ;
    le 2ᵉ nombre est la référence Apimo et doit être ignoré)."""
    m = re.search(r"€\s*([\d][\d.\xa0 ]*\d|\d)", text)
    if not m:
        return None
    cleaned = re.sub(r"[.\s\xa0 ]", "", m.group(1))
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    if v is not None and v < 1000:
        return None
    return v


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
    print(f"\nTotal Privilège Immo: {len(biens)} annonces")
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

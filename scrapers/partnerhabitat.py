"""scrapers/partnerhabitat.py — Part'ner Habitat (Sablé-sur-Sarthe, 72)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress, thème Estatik Pro « es- »).
URL pattern : /vente-immobiliere/?paged-1=N
              → liste de l'agence. Implantation mono-secteur Sablé-sur-Sarthe :
                tout le stock est en Sarthe (72). La carte de liste ne contient PAS
                le code postal ; on le récupère sur la PAGE DÉTAIL (adresse encodée
                dans l'iframe Google Maps « maps?q=...CP Ville ») puis on applique le
                POST-FILTRE strict CP[:2] → 0 fuite garanti.

Cartes liste : div.properties  (classe CSS porte aussi es_type-maison / es_type-...)
  - URL    : a[href*="/property/"]
  - Titre  : h2 a  (souvent = nom de commune)
  - Prix   : .es-price                 → "€ 228 800"
  - Surface: .es-bottom-icon--area / icône area  → "141 m²"
  - Type   : classe CSS es_type-{maison|appartement|terrain...}
  - Photo  : img[data-lazy-src]
Page détail :
  - CP+Ville : iframe ...maps?q=<adresse> 72300 Sablé-sur-Sarthe...

Type de bien : déduit de la classe CSS es_type-* (maison conservée, appart/terrain exclus).

Couverture : agence Sablé-sur-Sarthe (72) — bon stock local, prix souvent modestes.
             dernier_test 2026-06-09.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
from urllib.parse import unquote

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://partnerhabitat.com"
LIST_URL = BASE_URL + "/vente-immobiliere/"
MAX_PAGES = 6
PHOTOS_PER_CARD = 10


_KEEP_ES = re.compile(
    r"es_type-(maison|propriete|propriété|villa|ferme|longere|longère|manoir|"
    r"chateau|château|moulin|demeure|domaine|mas|pavillon|grange|fermette|bastide)",
    re.IGNORECASE,
)
_EXCLUDE_ES = re.compile(
    r"es_type-(appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|boutique|hangar|studio)",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1) collecte des cartes de liste (sans CP) filtrées sur prix/surface
        prelim: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            url = LIST_URL if page == 1 else f"{LIST_URL}?paged-1={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Partner] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.properties")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue
                if bien["id_annonce"] in seen_ids:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                seen_ids.add(bien["id_annonce"])
                prelim.append(bien)
                new_on_page += 1

            if new_on_page == 0 and page > 1:
                break
            await asyncio.sleep(0.5)

        # 2) résolution du CP/ville exact sur la fiche détail + POST-FILTRE STRICT
        for bien in prelim:
            cp, ville = await _fetch_cp(client, bien["url"])
            if not cp or cp[:2] not in departements:
                continue
            bien["code_postal"] = cp
            bien["departement"] = cp[:2]
            if ville:
                bien["ville"] = ville[:80]
            results.append(bien)
            await asyncio.sleep(0.4)

    print(f"[Partner] Total: {len(results)} annonces")
    return results


def _parse_card(card) -> dict | None:
    classes = " ".join(card.get("class", []))
    if _EXCLUDE_ES.search(classes):
        return None
    if not _KEEP_ES.search(classes):
        return None
    type_bien = _KEEP_ES.search(classes).group(1).lower()

    link = card.select_one('a[href*="/property/"]')
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    title_el = card.select_one("h2 a") or card.select_one(".es-property-link")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    price_el = card.select_one(".es-price")
    prix = _parse_price(price_el.get_text(" ", strip=True)) if price_el else None

    surface = _parse_surface(card.get_text(" ", strip=True))
    chambres = _parse_chambres(card)

    excerpt_el = card.select_one(".es-property-excerpt")
    description = excerpt_el.get_text(" ", strip=True) if excerpt_el else ""

    id_annonce = _id_from_classes(card) or url

    photos = []
    img = card.select_one("img")
    if img:
        src = img.get("data-lazy-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            photos.append(src)

    return {
        "source": "partnerhabitat",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": "",
        "ville": "",
        "code_postal": "",
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Part'ner Habitat",
    }


async def _fetch_cp(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """Récupère (CP, ville) depuis l'adresse encodée dans l'iframe Google Maps."""
    try:
        r = await client.get(url)
    except Exception:
        return "", ""
    if r.status_code != 200:
        return "", ""
    t = r.text
    # iframe src="https://maps.google.com/maps?q=49%20rue...%2072300%20Sabl..."
    m = re.search(r"maps\?q=([^&\"']+)", t)
    if m:
        q = unquote(m.group(1)).replace("+", " ")
        cp_m = re.search(r"\b(\d{5})\b", q)
        if cp_m:
            cp = cp_m.group(1)
            ville = q[cp_m.end():].strip(" ,")
            return cp, ville
    # repli : champ « Ville » Estatik
    m_v = re.search(r"Ville\s*<span[^>]*>:</span>\s*</strong>\s*([^<]+)", t)
    ville = m_v.group(1).strip().title() if m_v else ""
    # repli CP : 1ʳᵉ occurrence d'un 5-chiffres dans le bloc adresse
    m_cp = re.search(r"\b(\d{5})\b\s*" + re.escape(ville), t) if ville else None
    cp = m_cp.group(1) if m_cp else ""
    return cp, ville


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_surface(text: str) -> float | None:
    for cand in re.finditer(r"(\d{2,4}(?:[.,]\d+)?)\s*m²", text):
        prefix = text[max(0, cand.start() - 12):cand.start()].lower()
        if "terrain" in prefix:
            continue
        val = re.sub(r"[\s\xa0]", "", cand.group(1)).replace(",", ".")
        try:
            f = float(val)
            if 8 <= f <= 2000:
                return f
        except ValueError:
            pass
    return None


def _parse_chambres(card) -> int | None:
    el = card.select_one(".es-meta-icon--bedrooms")
    if el and el.parent:
        m = re.search(r"(\d+)", el.parent.get_text(" ", strip=True))
        if m:
            return int(m.group(1))
    return None


def _id_from_classes(card) -> str:
    for cls in card.get("class", []):
        m = re.match(r"post-(\d+)", cls)
        if m:
            return m.group(1)
    return ""


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
    print(f"\nTotal Part'ner Habitat: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('chambres') or '?'}ch"
            f" — {b['type_bien']} — {b['ville']}"
        )

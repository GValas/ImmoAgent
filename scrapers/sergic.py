"""scrapers/sergic.py — Sergic (administrateur de biens / transaction)

Méthode : scrape_simple (httpx) — SSR HTML, page unique
URL listing : https://www.sergic.com/rechercher-un-bien/?transaction=vente
  → l'inventaire NATIONAL complet (~693 biens vente) est rendu en SSR dans une
    SEULE page (pas de pagination httpx). Le querystring `localisation=` est
    ignoré côté serveur (HTML identique), donc on POST-FILTRE par code_postal[:2].

Cartes : div.listing-biens__card
  - URL/fiche : a[href]  → /annonces-immobilieres/achat-{n}-pieces-{surf}-{cp}-{ville}-{id}/
  - typologie : span.listing-biens__card-bottom-typology  ("Maison", "T3", "Parking"...)
  - prix      : .listing-biens__card-bottom-fline span (2e)  ("237 000€")
  - loc       : .listing-biens__card-bottom-sline span (1er) ("49250 Beaufort En Vallee")
  - surface   : .listing-biens__card-bottom-sline span (2e)  ("153m²")
  - id/pieces : extraits du slug d'URL
  - photo     : img[src]  (chemin relatif → préfixé BASE_URL)

Le code postal figure dans la carte ET dans le slug d'URL → filtrage dept fiable,
0 fuite. Couverture cible MODESTE (gros du stock Lille/Nantes/Paris ; quelques
biens 45/49). On exclut parkings/garages/box/terrains/caves (pas des maisons).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.sergic.com"
LIST_URL = f"{BASE_URL}/rechercher-un-bien/?transaction=vente"
PHOTOS_PER_CARD = 1


# Typologies à exclure (pas des logements/maisons)
_EXCLUDE_TYPO = re.compile(
    r"parking|garage|box|cave|stationnement|terrain", re.IGNORECASE
)

# Typologie Sergic → type_bien normalisé
_TYPE_MAP = [
    (re.compile(r"château|chateau", re.IGNORECASE), "château"),
    (re.compile(r"manoir", re.IGNORECASE), "manoir"),
    (re.compile(r"longère|longere|chaumière|chaumiere", re.IGNORECASE), "longère"),
    (re.compile(r"propriété|propriete|demeure", re.IGNORECASE), "propriété"),
    (re.compile(r"villa", re.IGNORECASE), "villa"),
    (re.compile(r"pavillon", re.IGNORECASE), "maison"),
    (re.compile(r"immeuble", re.IGNORECASE), "immeuble"),
    (re.compile(r"maison", re.IGNORECASE), "maison"),
    (re.compile(r"duplex|triplex", re.IGNORECASE), "appartement"),
    (re.compile(r"^t\d|studio", re.IGNORECASE), "appartement"),
]


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
    ) as client:
        try:
            r = await client.get(LIST_URL)
            r.raise_for_status()
        except Exception as e:
            print(f"[Sergic] Erreur listing: {e}")
            return results

        cards = _parse_page(r.text)

    for bien in cards:
        cp = bien.get("code_postal") or ""
        dept = cp[:2] if len(cp) >= 2 else ""
        if departements and dept not in departements:
            continue
        bien["departement"] = dept

        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        aid = bien.get("id_annonce") or bien.get("url")
        if aid in seen_ids:
            continue
        seen_ids.add(aid)
        results.append(bien)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[Sergic] Dept {dept}: {n} annonces")

    return results


def _parse_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for card in soup.select("div.listing-biens__card"):
        try:
            bien = _parse_card(card)
            if bien:
                out.append(bien)
        except Exception:
            continue
    return out


def _parse_card(card) -> dict | None:
    a = card.select_one("a[href]")
    if not a or not a.get("href"):
        return None
    url = a["href"].strip()
    if not url.startswith("http"):
        url = BASE_URL + url

    # typologie (titre brut)
    typo_el = card.select_one(".listing-biens__card-bottom-typology")
    typo = typo_el.get_text(strip=True) if typo_el else ""
    if _EXCLUDE_TYPO.search(typo):
        return None

    # ── localisation : "49250 Beaufort En Vallee" + surface "153m²" ──
    sline = card.select(".listing-biens__card-bottom-sline span")
    code_postal = None
    ville = None
    surface = None
    if sline:
        loc_text = sline[0].get_text(" ", strip=True)
        m_cp = re.search(r"\b(\d{5})\b", loc_text)
        if m_cp:
            code_postal = m_cp.group(1)
            ville = loc_text.replace(code_postal, "").strip() or None
        if len(sline) > 1:
            surface = _parse_num(sline[1].get_text(strip=True))

    # ── prix : 2e span de la fline ("237 000€") ──
    prix = None
    fline = card.select(".listing-biens__card-bottom-fline span")
    if len(fline) > 1:
        prix = _parse_num(fline[1].get_text(strip=True))

    # ── enrichissement depuis le slug d'URL ──
    # .../achat-{n}-pieces-{surf}-{cp}-{ville-slug}-{id}/
    slug = url.split("/annonces-immobilieres/")[-1]
    if not code_postal:
        m_cp2 = re.search(r"-(\d{5})-", slug)
        if m_cp2:
            code_postal = m_cp2.group(1)

    pieces = None
    m_p = re.search(r"-(\d+)-pieces-", slug)
    if m_p:
        pieces = int(m_p.group(1))

    if surface is None:
        m_s = re.search(r"-pieces-(\d+)-\d{5}-", slug)
        if m_s:
            surface = float(m_s.group(1))

    # id annonce : dernier segment alphanum du slug (ex. "oue1156", "idf53480")
    id_annonce = None
    m_id = re.search(r"-([a-z]+\d+[a-z]?)/?$", slug)
    if m_id:
        id_annonce = m_id.group(1)

    # type de bien normalisé
    type_bien = "maison"
    for rx, label in _TYPE_MAP:
        if rx.search(typo):
            type_bien = label
            break

    titre = f"{typo} à {ville}".strip() if ville else typo
    titre = re.sub(r"\s+", " ", titre).strip()

    # photo
    photos = []
    img = card.select_one("img[src]")
    if img:
        src = img.get("src") or ""
        if src:
            if not src.startswith("http"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "sergic",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": None,
        "departement": (code_postal or "")[:2] or None,
        "ville": ville,
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "dpe": None,
        "photos": photos,
        "agence": "Sergic",
    }


def _parse_num(text: str) -> float | None:
    """'237 000€' / '153m²' → float"""
    cleaned = re.sub(r"[^\d,\.]", "", (text or "").replace("\xa0", "").replace(" ", ""))
    cleaned = cleaned.replace(",", ".")
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    biens = asyncio.run(
        search({
            "departements": criteres.departements,
            "prix_max": criteres.prix_max,
            "prix_min": getattr(criteres, "prix_min", 0),
            "surface_min": criteres.surface_min,
        })
    )
    print(f"\nTotal Sergic (depts cibles): {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens})
    print(f"Départements vus: {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface', '?')}m²"
            f" — {b.get('pieces', '?')}p"
            f" — {b['ville']} ({b['type_bien']})"
        )

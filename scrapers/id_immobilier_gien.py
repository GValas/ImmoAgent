"""scrapers/id_immobilier_gien.py — ID Immobilier (Gien / Briare, Loiret 45)

Méthode : scrape_simple (httpx) — SSR HTML (thème WordPress « Realteo / MyHome »).
URL : page d'accueil https://www.id-immobilier.immo/ qui rend TOUTES les annonces
      actives « en vente » côté serveur (la page /nos-biens-en-vente/ est, elle,
      rendue en JS → on l'évite). Agence centrée sur le Giennois (Loiret 45).
Cartes : article.mh-estate-vertical
  IMPORTANT : le code postal et la ville sont encodés DANS LA CLASSE de l'article :
    mh-attribute-zip-code__45250 / mh-attribute-city__ouzouer-sur-trezee
  → filtre département STRICT et fiable via code_postal[:2] (0 fuite garanti).
  a.mh-thumbnail[title]        → titre + URL détail (/Biens immobiliers/maison/...)
  texte « NNN.NNN€ »           → prix
  .mh-estate-vertical__more-info → « Chambre: N », « Surface habitable: NNN m² »

Interface : async def search(criteres: dict) -> list[dict]
"""
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client

BASE_URL = "https://www.id-immobilier.immo"
SOURCE = "id_immobilier_gien"
LABEL = "IDImmobilierGien"
AGENCE = "ID Immobilier"

_EXCLUDE_TYPE = re.compile(r"property-type__(appartement|terrain|immeuble|local|commerce|garage|parking|fonds)")
# Le site mélange ventes ET locations sur l'accueil → on n'garde que les ventes.
_LOCATION = re.compile(r"offer-type__(en-location|location|loue|louer)")


def _price_fr(text: str) -> float | None:
    """« 202.000€ » → 202000 (le point est un séparateur de milliers FR)."""
    cleaned = re.sub(r"[€\s\xa0]", "", text or "")
    cleaned = re.sub(r"[^\d.,]", "", cleaned)
    # point/virgule séparateurs de milliers (groupes de 3) → on les retire
    cleaned = re.sub(r"[.,](?=\d{3}\b)", "", cleaned)
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen: set[str] = set()

    async with make_client() as client:
        r = await get_with_retry(client, f"{BASE_URL}/")
        if r is None or r.status_code != 200:
            print(f"[{LABEL}] accueil inaccessible (status {getattr(r, 'status_code', '?')})")
            return []
        cards = BeautifulSoup(r.text, "html.parser").select("article.mh-estate-vertical")
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue
            cp = bien.get("code_postal") or ""
            if not cp or cp[:2] not in departements:
                continue   # POST-FILTRE STRICT département (0 fuite)
            bien["departement"] = cp[:2]
            aid = bien["id_annonce"]
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

    print(f"[{LABEL}] {len(results)} annonces (post-filtre dept)")
    return results


def _parse_card(card) -> dict | None:
    classes = " ".join(card.get("class") or [])
    if _EXCLUDE_TYPE.search(classes) or _LOCATION.search(classes):
        return None   # appartement/terrain… ou LOCATION → ignorer (on ne garde que la vente)

    m_zip = re.search(r"zip-code__(\d{5})", classes)
    cp = m_zip.group(1) if m_zip else ""
    m_city = re.search(r"city__([a-z0-9-]+)", classes)
    ville = m_city.group(1).replace("-", " ").title() if m_city else ""

    link = card.select_one("a.mh-thumbnail") or card.find("a", href=True)
    if not link:
        return None
    href = link.get("href", "")
    url = href if href.startswith("http") else BASE_URL + href
    titre = link.get("title") or ""
    if not titre:
        titre = f"Maison {ville}".strip()
    id_annonce = href or titre

    prix = None
    for txt in card.find_all(string=re.compile("€")):
        prix = _price_fr(txt)
        if prix:
            break

    surface = chambres = None
    for info in card.select(".mh-estate-vertical__more-info"):
        t = info.get_text(" ", strip=True)
        m_s = re.search(r"Surface\s*habitable\s*:?\s*([\d\s\xa0]+)\s*m", t, re.IGNORECASE)
        if m_s:
            try:
                surface = float(re.sub(r"[\s\xa0]", "", m_s.group(1)))
            except ValueError:
                pass
        m_c = re.search(r"Chambre\s*:?\s*(\d+)", t, re.IGNORECASE)
        if m_c:
            chambres = int(m_c.group(1))

    photos = []
    img = card.find("img")
    if img:
        srcset = img.get("data-srcset") or ""
        src = img.get("data-src") or img.get("src") or ""
        if srcset:
            src = srcset.split()[0]
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)

    return {
        "source": SOURCE,
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": "",
        "departement": cp[:2] if cp else "",
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": AGENCE,
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "ID Immobilier Gien")

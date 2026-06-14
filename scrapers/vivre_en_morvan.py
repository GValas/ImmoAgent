"""scrapers/vivre_en_morvan.py — Vivre en Morvan (Anost, agence rurale Morvan)

Méthode : scrape_simple (httpx) — SSR HTML
Site : https://www.vivre-en-morvan.fr — agence immobilière indépendante d'Anost
spécialisée dans la vente de biens RURAUX du Morvan (longères, fermes, propriétés
de caractère). Couvre la Nièvre (58), la Saône-et-Loire (71), la Côte-d'Or (21)
et le sud Yonne (89).

URL : /acheter/ — page de liste SSR UNIQUE (tout le catalogue, ~50 cartes ; les
variantes ?page=N / page/2/ ne paginent pas → tout est servi en une page).
Aucun filtre département côté serveur → on parcourt le catalogue complet et on
POST-FILTRE STRICTEMENT sur code_postal[:2] (imprimé dans chaque carte, ex.
« Villapourçon (58370) ») contre les départements cibles → 0 fuite (le Morvan
déborde sur 71/21, ces biens sont écartés).

Cartes : <article> Tailwind contenant prix, « Ville (CP) », Surface, Nb de pièces,
Nb de chambres, Terrain et un lien /acheter/{slug-id}/.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_price

BASE_URL = "https://www.vivre-en-morvan.fr"
LIST_URL = f"{BASE_URL}/acheter/"
PHOTOS_PER_CARD = 3

_EXCLUDE_TYPE = re.compile(
    r"terrain|garage|parking|local|commerce|immeuble|bureau|fonds|appartement",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_min = criteres.get("prix_min", 0)
    prix_max = criteres.get("prix_max", 0)
    surface_min = criteres.get("surface_min", 0)
    results: list[dict] = []

    async with make_client() as client:
        r = await get_with_retry(client, LIST_URL)
        if r is None or r.status_code != 200:
            print(f"[VivreEnMorvan] liste inaccessible (status {getattr(r, 'status_code', '?')})")
            return results

        soup = BeautifulSoup(r.text, "html.parser")
        cards = [a for a in soup.find_all("article")
                 if a.select_one('a[href*="/acheter/"]')
                 and re.search(r"\(\d{5}\)", a.get_text())]
        seen: set[str] = set()
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue
            dept = (bien.get("code_postal") or "")[:2]
            if dept not in departements:
                continue
            aid = bien.get("id_annonce") or bien.get("url")
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

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print(f"[VivreEnMorvan] {len(results)} annonces — {by_dept}")
    await asyncio.sleep(0)
    return results


def _num(text: str, pattern: str) -> float | None:
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    try:
        return float(re.sub(r"[\s\xa0 ]", "", m.group(1)))
    except ValueError:
        return None


def _parse_card(card) -> dict | None:
    link = card.select_one('a[href*="/acheter/"]')
    href = link.get("href", "")
    url = href if href.startswith("http") else BASE_URL + href
    m_id = re.search(r"-(\d+)/?$", href.rstrip("/"))
    id_annonce = m_id.group(1) if m_id else url

    text = card.get_text(" ", strip=True)

    # Écarter les biens vendus / sous compromis / sous offre
    if re.search(r"\b(VENDU|Sous[- ]compromis|SOUS OFFRE|Vendu par)\b", text, re.IGNORECASE):
        return None

    # Ville (CP)
    m_loc = re.search(r"([A-Za-zÀ-ÿ'’\-\. ]+?)\s*\((\d{5})\)", text)
    if not m_loc:
        return None
    ville = m_loc.group(1).strip()
    # nettoyer les badges résiduels (Exclusivité, Nouveauté…) collés au nom de ville
    ville = re.sub(r"^(Exclusivit[ée]|Nouveaut[ée]|Coup de c(?:œ|oe)ur)\s+", "", ville,
                   flags=re.IGNORECASE).strip()
    code_postal = m_loc.group(2)

    prix = None
    m_prix = re.search(r"([\d\s\xa0 ]{4,})\s*€", text)
    if m_prix:
        prix = parse_price(m_prix.group(1))

    surface = _num(text, r"Surface\s+([\d\s\xa0 ]+)\s*m")
    pieces = _num(text, r"Nb de pi[èe]ces\s+(\d+)")
    chambres = _num(text, r"Nb de chambres\s+(\d+)")
    surface_terrain = _num(text, r"Terrain\s+([\d\s\xa0 ]+)\s*m")

    # Type depuis le slug d'URL (longere, maison, propriete, ferme…)
    slug = href.rstrip("/").split("/")[-1]
    type_bien = "maison"
    for t in ("longere", "ferme", "propriete", "manoir", "moulin", "chateau",
              "maison", "domaine", "grange", "corps-de-ferme"):
        if t in slug:
            type_bien = t.replace("-", " ")
            break
    if _EXCLUDE_TYPE.search(slug):
        return None

    titre = link.get("title") or ""
    if not titre:
        # texte du lien « Découvrir » pas pertinent → reconstruire
        titre = f"{type_bien.title()} {ville}".strip()

    photos = []
    for img in card.select("img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "vivre_en_morvan",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": int(pieces) if pieces else None,
        "chambres": int(chambres) if chambres else None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Vivre en Morvan",
    }


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Vivre en Morvan")

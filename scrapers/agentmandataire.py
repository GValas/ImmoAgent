"""scrapers/agentmandataire.py — Agent Mandataire France (réseau de mandataires)

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /annonces-immobilieres/page-{N}?localization={NN}&new_search=1
              → le paramètre `localization` accepte un code département (ex: 45)
                et filtre CÔTÉ SERVEUR (vérifié : aucune fuite hors-dept).
              Post-filtre strict code_postal[:2] == dept conservé par sécurité.

Cartes : a.card.card-job (9/page max, pagination /page-N)
  - URL    : href de la carte → /annonces/{ville}-{CP}/{type-slug}/{id}
  - id     : dernier segment numérique de l'URL ; secours = .mandate "Mandat : XXXX"
  - Ville  : .city (texte hors .zipcode)
  - CP     : .city .zipcode  →  "45160"
  - Type   : .type           →  "Maison", "Maison de village", "Terrain constructible"...
  - Prix   : .price          →  "508 800 €"
  - Extras : .extras         →  "190.7 m²  • 7 pièce(s)  • 4 chambre(s)"  (terrain seul pour terrains)
  - Photo  : .card-img img[src]
  - Agence : .agent .name (mandataire) ; agence = "Agent Mandataire France"

Type de bien : on ne garde que maisons / propriétés (exclut appartement, terrain,
               commerce, immeuble, parking...).

Couverture : réseau national à implantation inégale ; sur les départements cibles
             le stock est faible mais réel (45 le mieux fourni).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int

BASE_URL = "https://www.agentmandataire.fr"
MAX_PAGES = 10
PHOTOS_PER_CARD = 10


# Types de bien (à partir du libellé .type ou du slug d'URL) à conserver
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps.de.ferme|"
    r"maison.de.village|maison.de.ville|chalet|bastide|grange",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"viager|loft|studio|hangar|cave",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            try:
                biens = await _scrape_dept(
                    client, dept, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[AgentMandataire] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[AgentMandataire] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = (
            f"{BASE_URL}/annonces-immobilieres/page-{page}"
            f"?localization={dept}&new_search=1"
        )
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select("a.card.card-job")
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            try:
                bien = _parse_card(card, dept)
            except Exception:
                continue
            if not bien:
                continue

            # Post-filtre dept STRICT (le filtre serveur est déjà fiable)
            if bien["code_postal"] and bien["code_postal"][:2] != dept:
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
            new_on_page += 1

        if new_on_page == 0:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    href = card.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien : libellé .type (secours = slug d'URL)
    type_el = card.select_one(".type")
    type_label = type_el.get_text(" ", strip=True) if type_el else ""
    parts = [p for p in href.split("/") if p]
    type_slug = parts[-2] if len(parts) >= 2 else ""
    type_probe = f"{type_label} {type_slug}"
    if _EXCLUDE_TYPE.search(type_probe) and not _KEEP_TYPE.search(type_probe):
        return None
    if not _KEEP_TYPE.search(type_probe):
        # type inconnu/ambigu → exclu par prudence
        return None
    type_bien = type_label or type_slug.replace("-", " ").strip() or "maison"

    # id_annonce : dernier segment numérique de l'URL ; secours = mandat
    id_annonce = ""
    if parts and re.fullmatch(r"\d+", parts[-1]):
        id_annonce = parts[-1]
    if not id_annonce:
        mand_el = card.select_one(".mandate")
        if mand_el:
            m = re.search(r"([A-Za-z0-9]+)", mand_el.get_text("", strip=True)
                          .replace("Mandat", "").replace(":", ""))
            if m:
                id_annonce = m.group(1)
    id_annonce = id_annonce or url

    # Localisation : .city contient le nom de ville + <span.zipcode>
    city_el = card.select_one(".city")
    code_postal = ""
    ville = ""
    if city_el:
        zip_el = city_el.select_one(".zipcode")
        if zip_el:
            code_postal = zip_el.get_text(strip=True)
            zip_el.extract()
        ville = city_el.get_text(" ", strip=True)
    if not code_postal:
        m = re.search(r"-(\d{5})/", href)
        if m:
            code_postal = m.group(1)

    # Prix
    price_el = card.select_one(".price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Extras : "190.7 m²  • 7 pièce(s)  • 4 chambre(s)"
    extras_el = card.select_one(".extras")
    extras = extras_el.get_text(" ", strip=True) if extras_el else ""
    surface = _parse_surface(extras)
    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", extras)
    chambres = _parse_int(r"(\d+)\s*chambre", extras)

    # Titre (pas de titre libre sur la carte → reconstruit)
    titre = f"{type_bien} {ville}".strip()
    if surface:
        titre += f" {int(surface)} m²"

    # Photo principale
    photos = []
    img = card.select_one(".card-img img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    # Mandataire
    agent_el = card.select_one(".agent .name")
    mandataire = agent_el.get_text(" ", strip=True) if agent_el else ""
    agence = "Agent Mandataire France"
    if mandataire:
        agence = f"Agent Mandataire France — {mandataire}"

    return {
        "source": "agentmandataire",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": agence,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text.replace("\xa0", " "))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'190.7 m²  • 7 pièce(s)' → 190.7 (première surface en m²)."""
    if not text:
        return None
    m = re.search(r"([\d]+(?:[.,]\d+)?)\s*m²?", text)
    if m:
        try:
            f = float(m.group(1).replace(",", "."))
            if 5 <= f <= 5000:
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
    print(f"\nTotal Agent Mandataire: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

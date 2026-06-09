"""scrapers/agence_du_soleil.py — Agence du Soleil (agence régionale Occitanie)

Méthode : scrape_simple (httpx) — SSR HTML
Couverture : Aude (11), Hérault (34), Pyrénées-Orientales (66) UNIQUEMENT.
             Agence mono-région : aucun stock hors de ces 3 départements.

URL pattern (listing national multi-dept) :
    /annonces-immobilieres/vente/{type}/            (page 1)
    /annonces-immobilieres/vente/{type}/page-{N}/   (pages suivantes)
  types scrapés : maison, appartement, terrain.

Filtre département : le site n'expose AUCUN code postal (ni sur la carte ni sur
  la page détail — le JSON-LD ne contient que `addressLocality`). Il n'y a pas
  non plus d'URL/param par code département : le filtre serveur se fait par nom
  de ville (`ville[]`). On scrape donc le listing complet (les 3 depts mélangés)
  et on POST-FILTRE STRICTEMENT via une table ville→département (CITY_DEPT,
  résolue via geo.api.gouv.fr). Toute ville inconnue est EXCLUE par prudence →
  0 fuite garantie. Comme la zone test (72/28/45/89, Val-de-Loire) est disjointe
  des 3 depts couverts, ce scraper renvoie 0 bien hors de sa région.

Cartes : article.card (schema.org/RealEstateListing), enveloppées dans
         a.card-link[href].
  - URL    : a.card-link[href]  → /annonce-immobiliere/vente/{type}/ref-...-AGENCEDUSOLEIL/
  - Titre  : h3.card-title       → "Ville - Vente Maison - 60 m²"
  - Type+ville : p.card-type     → "Maison Port-la-Nouvelle"
  - Prix   : p.card-price[content] (ex 149800.00) sinon texte "149 800 €"
  - Photo  : .card-image img[src] (+ srcset)
  - Footer : .card-footer span   → [surface, (pieces?), chambres]  (variable)

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import unicodedata

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.agencedusoleil.com"
MAX_PAGES = 20
PHOTOS_PER_CARD = 1  # une seule photo exposée par carte sur le listing

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien (segment d'URL) à scraper
TYPE_SLUGS = ["maison", "appartement", "terrain"]

# Table ville (normalisée) → code département.
# Résolue depuis la liste `ville[]` du site via geo.api.gouv.fr.
# Toute ville absente de cette table est EXCLUE (sécurité anti-fuite).
_CITY_DEPT_RAW: dict[str, str] = {
    "Argelès-sur-Mer": "66", "Argens-Minervois": "11", "Baho": "66",
    "Baixas": "66", "Banyuls-sur-Mer": "66", "Béziers": "34", "Bizanet": "11",
    "Boujan-sur-Libron": "34", "Canet Plage": "66", "Canet-en-Roussillon": "66",
    "Canohès": "66", "Cap d'Agde": "34", "Agde": "34", "Caves": "11",
    "Collioure": "66", "Conilhac-Corbières": "11", "Corneilla-del-Vercol": "66",
    "Coursan": "11", "Cuxac-d'Aude": "11", "Elne": "66",
    "Espira-de-l'Agly": "66", "Estagel": "66", "Fabrezan": "11",
    "Ferrals-les-Corbières": "11", "Fleury": "11",
    "Font-Romeu-Odeillo-Via": "66", "Gabian": "34", "Gruissan": "11",
    "La Grande-Motte": "34", "La Palme": "11", "Lagrasse": "11",
    "Latour-Bas-Elne": "66", "Latour-de-France": "66", "Le Barcarès": "66",
    "Les Angles": "66", "Leucate": "11", "Lézignan-Corbières": "11",
    "Luc-sur-Orbieu": "11", "Marcorignan": "11", "Montbrun-des-Corbières": "11",
    "Montpellier": "34", "Moussan": "11", "Narbonne": "11",
    "Narbonne-Plage": "11", "Nissan-lez-Enserune": "34", "Opoul-Périllos": "66",
    "Ornaisons": "11", "Paraza": "11", "Perpignan": "66",
    "Peyriac-de-Mer": "11", "Pézenas": "34", "Port Leucate": "11",
    "Port-la-Nouvelle": "11", "Portel-des-Corbières": "11", "Puisserguier": "34",
    "Rivesaltes": "66", "Roquefort-des-Corbières": "11",
    "Saint-André-de-Roquelongue": "11", "Saint-Cyprien": "66",
    "Saint-Estève": "66", "Saint-Hippolyte": "66", "Saint-Jean-Lasseille": "66",
    "Saint-Laurent-de-la-Salanque": "66", "Saint-Marcel-sur-Aude": "11",
    "Saint-Nazaire": "66", "Saint-Pierre-la-Mer": "11",
    "Sainte-Marie-la-Mer": "66", "Sauvian": "34", "Sérignan": "34", "Sète": "34",
    "Sigean": "11", "Théza": "66", "Thézan-lès-Béziers": "34", "Torreilles": "66",
    "Tourouzelle": "11", "Valras-Plage": "34", "Villeneuve-de-la-Raho": "66",
    "Villerouge": "11", "Villesèque-des-Corbières": "11", "Vinassan": "11",
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


# Table normalisée pour lookup robuste
CITY_DEPT: dict[str, str] = {_norm(k): v for k, v in _CITY_DEPT_RAW.items()}

# Départements réellement couverts par l'agence
COVERED_DEPTS = {"11", "34", "66"}


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Optimisation : si aucun département cible n'est couvert, rien à scraper.
    cibles = set(departements) & COVERED_DEPTS
    if not cibles:
        print(
            f"[AgenceDuSoleil] Aucun département cible dans la zone couverte "
            f"(11/34/66) — 0 annonce. (cibles demandées : {departements})"
        )
        return []

    results: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for type_slug in TYPE_SLUGS:
            try:
                biens = await _scrape_type(
                    client, type_slug, cibles, seen, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[AgenceDuSoleil] {type_slug}: {len(biens)} annonces (zone)")
            except Exception as e:
                print(f"[AgenceDuSoleil] Erreur {type_slug}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_type(
    client: httpx.AsyncClient,
    type_slug: str,
    cibles: set[str],
    seen: set[str],
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []

    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = f"{BASE_URL}/annonces-immobilieres/vente/{type_slug}/"
        else:
            url = f"{BASE_URL}/annonces-immobilieres/vente/{type_slug}/page-{page}/"

        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select("article.card")
        if not cards:
            break

        for card in cards:
            try:
                bien = _parse_card(card, type_slug)
            except Exception:
                continue
            if not bien:
                continue

            dept = bien["departement"]
            # Post-filtre STRICT : ville inconnue (dept None) ou hors cible → drop
            if dept not in cibles:
                continue

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
            biens.append(bien)

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card, type_slug: str) -> dict | None:
    link = card.find_parent("a", class_="card-link")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce depuis la ref de l'URL : .../ref-10-11273-AGENCEDUSOLEIL/
    m_ref = re.search(r"ref-([\w-]+?)-AGENCEDUSOLEIL", href)
    id_annonce = m_ref.group(1) if m_ref else url

    # Type de bien
    type_bien = type_slug

    # Ville : p.card-type = "Maison Port-la-Nouvelle" → on retire le mot type.
    type_el = card.select_one(".card-type")
    type_txt = type_el.get_text(" ", strip=True) if type_el else ""
    ville = re.sub(
        r"^(maison|appartement|terrain|immeuble|stationnement|immobilier\s+pro)\s+",
        "",
        type_txt,
        flags=re.IGNORECASE,
    ).strip()

    # Secours : ville depuis le titre "Ville - Vente Maison - 60 m²"
    title_el = card.select_one(".card-title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not ville and titre:
        ville = titre.split(" - ")[0].strip()

    departement = CITY_DEPT.get(_norm(ville)) if ville else None

    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Prix : attribut content (ex 149800.00) prioritaire, sinon texte
    price_el = card.select_one(".card-price")
    prix = None
    if price_el:
        content = price_el.get("content")
        if content:
            try:
                prix = float(content)
            except ValueError:
                prix = None
        if prix is None:
            prix = _parse_price(price_el.get_text(" ", strip=True))

    # Footer : surface (m²) puis pièces/chambres (nb de spans variable)
    surface = None
    pieces = None
    chambres = None
    nums: list[int] = []
    for sp in card.select(".card-footer span"):
        t = sp.get_text(" ", strip=True)
        m_m2 = re.search(r"([\d\s\xa0]+)\s*m²", t)
        if m_m2 and surface is None:
            surface = _to_float(m_m2.group(1))
            continue
        m_n = re.search(r"\b(\d+)\b", t)
        if m_n:
            nums.append(int(m_n.group(1)))
    # nums = [chambres] ou [pieces, chambres]
    if len(nums) == 1:
        chambres = nums[0]
    elif len(nums) >= 2:
        pieces = nums[0]
        chambres = nums[1]

    # Surface en secours depuis le titre "... - 60 m²"
    if surface is None and titre:
        m_t = re.search(r"([\d\s\xa0]+)\s*m²", titre)
        if m_t:
            surface = _to_float(m_t.group(1))

    # Photo
    photos = []
    img = card.select_one(".card-image img")
    if img:
        src = img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "agence_du_soleil",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": departement,
        "ville": ville[:80],
        "code_postal": "",  # site n'expose aucun CP
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Agence du Soleil",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _to_float(text: str) -> float | None:
    val = re.sub(r"[\s\xa0]", "", text)
    try:
        return float(val) if val else None
    except ValueError:
        return None


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d.]", "", re.sub(r"[€\s\xa0]", "", text).replace(",", "."))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
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
    print(f"\nTotal Agence du Soleil: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

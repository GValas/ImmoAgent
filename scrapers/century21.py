"""scrapers/century21.py — Century 21 France (réseau ~960 agences)

Méthode : scrape_simple (httpx) — SSR HTML (Tailwind, pas de Cloudflare, pas de JS).

⚠ NB : l'ancien blacklist Century21 visait /recherche/achat/... (404 "page n'existe
plus"). Le VRAI listing pagine est :
    /annonces/achat-maison/d-{NN}_{dept_slug}/         (page 1)
    /annonces/achat-maison/d-{NN}_{dept_slug}/page-{N}/  (suivantes)
Filtre département CÔTÉ SERVEUR FIABLE via le segment d-{NN}_ (vérifié sur les 11
depts cibles : le token dept de chaque carte == dept demandé, 0 fuite). 20 cartes/page,
pagination réelle (page-2 = 20 nouvelles annonces).

Cartes : div.js-the-list-of-properties-list-property
  Lien   : a[href^='/trouver_logement/detail/{id}/']
  Texte  : "VILLE {NN} 168,14 m² , 4 pièces Ref : 44355 Maison à vendre 161 000 € ..."
  Photo  : img[src] /imagesBien/...

Le code postal complet n'est pas dans la carte (ville + dept 2 chiffres) → code_postal
laissé vide, departement = token dept (fiable). Pièces/surface/prix/réf depuis le texte.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.century21.fr"
MAX_PAGES = 12
PHOTOS_PER_CARD = 6

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

DEPT_SLUGS: dict[str, str] = {
    "72": "72_sarthe",
    "28": "28_eure_et_loir",
    "45": "45_loiret",
    "89": "89_yonne",
    "49": "49_maine_et_loire",
    "37": "37_indre_et_loire",
    "36": "36_indre",
    "18": "18_cher",
    "58": "58_nievre",
    "41": "41_loir_et_cher",
    "53": "53_mayenne",
}


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
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, slug, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[Century21] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Century21] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.5)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    slug: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = f"{BASE_URL}/annonces/achat-maison/d-{slug}/"
        else:
            url = f"{BASE_URL}/annonces/achat-maison/d-{slug}/page-{page}/"
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select(
            "div.js-the-list-of-properties-list-property"
        )
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            bien = _parse_card(card, dept)
            if not bien:
                continue
            if bien["id_annonce"] in seen:
                continue

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            seen.add(bien["id_annonce"])
            biens.append(bien)
            new_on_page += 1

        if new_on_page == 0:
            break
        await asyncio.sleep(0.4)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    a = card.find("a", href=re.compile(r"/trouver_logement/detail/\d+/"))
    if not a:
        return None
    href = a["href"]
    url = href if href.startswith("http") else BASE_URL + href
    m_id = re.search(r"/detail/(\d+)/", href)
    id_annonce = m_id.group(1) if m_id else url

    text = re.sub(r"\s+", " ", card.get_text(" ", strip=True))

    # "VILLE {NN} 168,14 m² , 4 pièces ..." — ville = avant le token dept
    ville = ""
    m_loc = re.search(r"^(?:Exclusivité\s+)?(.+?)\s+" + re.escape(dept) + r"\s+[\d,.\s]+m", text)
    if m_loc:
        ville = m_loc.group(1).strip().title()

    # Sécurité dept : le token dept doit être présent dans la carte
    if not re.search(r"\b" + re.escape(dept) + r"\s+[\d,.\s]+m", text):
        return None

    surface = _surface(text, dept)
    pieces = _int(r"(\d+)\s*pi[eè]ces?", text)
    prix = _price(text)
    ref = None
    m_ref = re.search(r"Ref\s*:?\s*(\w+)", text)
    if m_ref:
        ref = m_ref.group(1)

    # Type
    type_bien = "maison"
    m_type = re.search(
        r"\b(villa|propri[eé]t[eé]|ch[aâ]teau|manoir|long[eè]re|ferme|moulin|maison)\b",
        text, re.IGNORECASE,
    )
    if m_type:
        type_bien = m_type.group(1).lower().replace("chateau", "château").replace(
            "propriete", "propriété"
        )

    # Description : après le prix
    desc = ""
    m_desc = re.search(r"€\s*(?:Visiter le site dédié\s*)?(.+)$", text)
    if m_desc:
        desc = m_desc.group(1).strip()

    titre = f"{type_bien.title()}"
    if pieces:
        titre += f" {pieces} pièces"
    if surface:
        titre += f" {int(surface)}m²"
    if ville:
        titre += f" {ville}"

    photos = []
    for img in card.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original") or ""
        if "/imagesBien/" in src and not src.startswith("data:"):
            full = src if src.startswith("http") else BASE_URL + src
            if full not in photos:
                photos.append(full)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "century21",
        "url": url,
        "id_annonce": ref or id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": desc[:1000],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",          # CP complet absent de la liste (ville + dept seul)
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Century 21",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _surface(text: str, dept: str) -> float | None:
    # "VILLE {dept} 168,14 m²" — on prend le nombre juste après le token dept
    m = re.search(re.escape(dept) + r"\s+([\d.,\s]+?)\s*m", text)
    if not m:
        m = re.search(r"([\d][\d.,\s]*)\s*m²", text)
    if not m:
        return None
    raw = m.group(1).replace(" ", "").replace("\xa0", "")
    raw = raw.replace(",", ".")
    # garder un seul point décimal
    raw = re.sub(r"\.(?=.*\.)", "", raw)
    try:
        f = float(raw)
        return f if 8 <= f <= 3000 else None
    except ValueError:
        return None


def _int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _price(text: str) -> float | None:
    m = re.search(r"à vendre\s+([\d][\d\s\xa0]{2,})\s*€", text, re.IGNORECASE)
    if not m:
        m = re.search(r"([\d][\d\s\xa0]{4,})\s*€", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
        return f if 5000 < f < 30_000_000 else None
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
                "departements": criteres.departements[:4],
                "prix_max": criteres.prix_max,
                "prix_min": getattr(criteres, "prix_min", 0),
                "surface_min": criteres.surface_min,
            }
        )
    )
    print(f"\nTotal Century 21: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — photos {len(b['photos'])} — {b['ville']}"
        )

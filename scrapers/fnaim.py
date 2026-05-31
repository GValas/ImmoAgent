"""scrapers/fnaim.py — FNAIM (portail des agences adhérentes FNAIM)

Méthode : scrape_simple (httpx) — SSR HTML (pas de Cloudflare, pas de JS).

⚠ NB : l'ancien blacklist FNAIM visait /annonces/achat/maison/ (qui redirige vers
l'accueil). Le VRAI listing pagine est :
    /liste-annonces-immobilieres/17-acheter-maison-{dept-slug}-{NN}.htm
    pagination : ...-{dept-slug}-{NN}-page-{N}.htm
Filtre département CÔTÉ SERVEUR (slug + code) FIABLE : 100% des CP exposés
appartiennent au dept demandé (vérifié sur les 11 depts cibles, 0 fuite).
Volume conséquent : 25 cartes/page, 18-34 pages/dept (agrège tous les adhérents FNAIM).

Cartes : div.itemInfo (le lien détail est /annonce-immobiliere/{id}/17-acheter-maison-{ville}-{CP}.htm)
  Texte carte : "Maison 4 pièces 49m² FRESNAY SUR SARTHE 72130 20 000€ 2 chambres ..."
  Quand le prix est masqué ("Nous consulter pour le prix"), ville/CP sont absents du
  texte → on prend TOUJOURS ville+CP depuis le slug de l'URL détail.
  Photos : imagesv2.fnaim.fr

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.fnaim.fr"
MAX_PAGES = 25            # plafond par dept (cap raisonnable ; 25 pages ≈ 625 annonces)
PHOTOS_PER_CARD = 8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

DEPT_SLUGS: dict[str, str] = {
    "72": "sarthe",
    "28": "eure-et-loir",
    "45": "loiret",
    "89": "yonne",
    "49": "maine-et-loire",
    "37": "indre-et-loir",   # corrigé ci-dessous via map réelle
    "36": "indre",
    "18": "cher",
    "58": "nievre",
    "41": "loir-et-cher",
    "53": "mayenne",
}
# slug exact (vérifié) — indre-et-loire
DEPT_SLUGS["37"] = "indre-et-loire"

_TYPE_RE = re.compile(
    r"\b(maison|villa|propri[eé]t[eé]|ch[aâ]teau|manoir|long[eè]re|ferme|moulin|"
    r"demeure|mas|domaine)\b",
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
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, slug, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[FNAIM] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[FNAIM] Erreur dept {dept}: {e}")
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
            url = f"{BASE_URL}/liste-annonces-immobilieres/17-acheter-maison-{slug}-{dept}.htm"
        else:
            url = (
                f"{BASE_URL}/liste-annonces-immobilieres/"
                f"17-acheter-maison-{slug}-{dept}-page-{page}.htm"
            )
        r = await client.get(url)
        if r.status_code != 200:
            break

        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("ul.liste li.item")
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            bien = _parse_card(card, dept)
            if not bien:
                continue
            if bien["id_annonce"] in seen:
                continue
            # Sécurité dept (filtre serveur déjà fiable)
            if bien["code_postal"] and bien["code_postal"][:2] != dept:
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
    a = card.find("a", href=re.compile(r"/annonce-immobiliere/\d+/"))
    if not a:
        a = card.find_previous("a", href=re.compile(r"/annonce-immobiliere/\d+/"))
    if not a:
        return None
    href = a["href"].split("#")[0]
    url = href if href.startswith("http") else BASE_URL + href
    m_id = re.search(r"/annonce-immobiliere/(\d+)/", href)
    id_annonce = m_id.group(1) if m_id else url

    # Ville + CP TOUJOURS depuis le slug d'URL (fiable même prix masqué)
    ville, code_postal = _loc_from_slug(href)
    if not code_postal:
        # secours : depuis le texte
        m_cp = re.search(r"\b(\d{5})\b", card.get_text(" ", strip=True))
        code_postal = m_cp.group(1) if m_cp else ""
    if not code_postal:
        # carte sans localisation exploitable (bloc agence/pub) → on écarte
        return None

    text = card.get_text(" ", strip=True)

    # On ne garde que les maisons/propriétés (l'URL contient déjà 'maison')
    type_bien = "maison"
    m_type = _TYPE_RE.search(text)
    if m_type:
        type_bien = m_type.group(1).lower()
        type_bien = type_bien.replace("chateau", "château").replace(
            "propriete", "propriété"
        )

    pieces = _int(r"(\d+)\s*pi[eè]ces?", text)
    chambres = _int(r"(\d+)\s*chambres?", text)
    surface = _surface(text)
    prix = _price(text)

    titre = f"{type_bien.title()}"
    if pieces:
        titre += f" {pieces} pièces"
    if surface:
        titre += f" {int(surface)}m²"
    titre += f" — {ville}".rstrip(" —")

    # Description (texte après le prix)
    desc = ""
    m_desc = re.search(r"€\s*(?:\d+\s*chambres?)?(.+)$", text)
    if m_desc:
        desc = m_desc.group(1).strip()
    if not desc:
        desc = text

    photos = []
    for img in card.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original") or ""
        if "fnaim.fr" in src and "/img/" in src and not src.startswith("data:"):
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "fnaim",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": desc[:1200],
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
        "agence": "Agence FNAIM",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _loc_from_slug(href: str) -> tuple[str, str]:
    """'/annonce-immobiliere/52632180/17-acheter-maison-st-calais-72120.htm'
       → ('St Calais', '72120')"""
    m = re.search(r"acheter-maison-(.+?)-(\d{5})\.htm", href)
    if not m:
        return "", ""
    ville = m.group(1).replace("-", " ").strip().title()
    return ville, m.group(2)


def _int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _surface(text: str) -> float | None:
    m = re.search(r"([\d\s\xa0]{1,7})\s*m²", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
        return f if 8 <= f <= 3000 else None
    except ValueError:
        return None


def _price(text: str) -> float | None:
    # "Nous consulter pour le prix" → None
    if re.search(r"consulter\s+pour\s+le\s+prix", text, re.IGNORECASE):
        return None
    # Le prix suit le CP : "... ST CALAIS 72120 18 500€". On capture les groupes de
    # chiffres précédant le € SANS avaler le code postal (5 chiffres collés).
    m = re.search(r"(\d{5})\s+([\d][\d\s\xa0]{0,9}?)\s*€", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(2))
    else:
        m2 = re.search(r"([\d][\d\s\xa0]{2,9}?)\s*€", text)
        val = re.sub(r"[\s\xa0]", "", m2.group(1)) if m2 else ""
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
    print(f"\nTotal FNAIM: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p/{b.get('chambres') or '?'}ch"
            f" — {b['ville']}"
        )

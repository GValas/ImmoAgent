"""scrapers/fnaim.py — FNAIM (portail des agences adhérentes FNAIM)

Méthode : scrape_simple (httpx) — SSR HTML (pas de Cloudflare, pas de JS).

⚠ NB : l'ancien blacklist FNAIM visait /annonces/achat/maison/ (qui redirige vers
l'accueil). Le VRAI listing pagine est :
    /liste-annonces-immobilieres/17-acheter-maison-{dept-slug}-{NN}.htm
    pagination : ...-{dept-slug}-{NN}-page-{N}.htm
Filtre département CÔTÉ SERVEUR (slug + code) FIABLE : 100% des CP exposés
appartiennent au dept demandé (vérifié sur les 11 depts cibles, 0 fuite).
Volume conséquent : 25 cartes/page, 18-34 pages/dept (agrège tous les adhérents FNAIM).

Cartes : ul.liste li.item (le lien détail est /annonce-immobiliere/{id}/17-acheter-maison-{ville}-{CP}.htm)
  Texte carte : "Maison 4 pièces 49m² FRESNAY SUR SARTHE 72130 20 000€ 2 chambres ..."
  Quand le prix est masqué ("Nous consulter pour le prix"), ville/CP sont absents du
  texte → on prend TOUJOURS ville+CP depuis le slug de l'URL détail.
  Photos : imagesv2.fnaim.fr

Migré sur scrapers/_base.py (modèle le_tuc.py) : HEADERS, map dept→slug, boucle
département + pagination, filtres prix/surface et dédup id viennent du socle. Ne
restent ici que le patron d'URL, le sélecteur de carte et le parsing des champs
(prix après CP, ville/CP depuis le slug détail).

Interface : async def search(criteres: dict) -> list[dict]
"""
import re

from scrapers._base import parse_int, run_dept_search, standalone_main

BASE_URL = "https://www.fnaim.fr"
MAX_PAGES = 25            # plafond par dept (cap raisonnable ; 25 pages ≈ 625 annonces)
PHOTOS_PER_CARD = 8

_TYPE_RE = re.compile(
    r"\b(maison|villa|propri[eé]t[eé]|ch[aâ]teau|manoir|long[eè]re|ferme|moulin|"
    r"demeure|mas|domaine)\b",
    re.IGNORECASE,
)


def _page_url(dept: str, slug: str, page: int) -> str:
    if page == 1:
        return f"{BASE_URL}/liste-annonces-immobilieres/17-acheter-maison-{slug}-{dept}.htm"
    return (
        f"{BASE_URL}/liste-annonces-immobilieres/"
        f"17-acheter-maison-{slug}-{dept}-page-{page}.htm"
    )


async def search(criteres: dict) -> list[dict]:
    return await run_dept_search(
        source="fnaim",
        label="FNAIM",
        page_url=_page_url,
        card_selector="ul.liste li.item",
        parse_card=_parse_card,
        criteres=criteres,
        max_pages=MAX_PAGES,
        page_sleep=0.4,
        dept_sleep=0.5,
    )


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

    pieces = parse_int(r"(\d+)\s*pi[eè]ces?", text)
    chambres = parse_int(r"(\d+)\s*chambres?", text)
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


# ── Helpers propres à FNAIM (formats non couverts par _base) ───────────────────

def _loc_from_slug(href: str) -> tuple[str, str]:
    """'/annonce-immobiliere/52632180/17-acheter-maison-st-calais-72120.htm'
       → ('St Calais', '72120')"""
    m = re.search(r"acheter-maison-(.+?)-(\d{5})\.htm", href)
    if not m:
        return "", ""
    ville = m.group(1).replace("-", " ").strip().title()
    return ville, m.group(2)


def _surface(text: str) -> float | None:
    """Surface = 1er 'NNN m²' du texte carte (pas de mot-clé 'hab', bornes 8-3000)."""
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


if __name__ == "__main__":
    standalone_main(search, "FNAIM")

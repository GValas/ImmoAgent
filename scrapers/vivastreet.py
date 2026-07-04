"""scrapers/vivastreet.py — Vivastreet (petites annonces, section immobilier vente, P2P)

Méthode : scrape_simple (httpx) — SSR HTML.
URL pattern : /immobilier-vente-maison/{slug-departement} (+ « /t+{N} » pour paginer,
              page 1 sans suffixe ; un dept sans page 2 → 302). Filtre département
              CÔTÉ SERVEUR par slug (sarthe, loiret…), légères fuites de depts
              limitrophes constatées → post-filtre CP strict (keep_bien).
Cartes : classes Tailwind volatiles → repérage robuste par les liens d'annonce
         a[href~ /immobilier-vente-maison/{ville-CP}/{slug}/{id}] puis remontée au
         <li> conteneur ; champs extraits du texte de la carte (prix « 450.000 € »,
         DPE, CP dans l'URL ou le texte).
Stock minuscule (1-3 annonces/dept) mais annonces DIRECT PARTICULIER introuvables
ailleurs (complément d'entreparticuliers).

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import (
    DEFAULT_DEPT_SLUGS,
    get_with_retry,
    keep_bien,
    make_client,
    parse_price_digits,
    parse_str_upper,
    standalone_main,
)

BASE_URL = "https://www.vivastreet.com"
MAX_PAGES = 3  # stock minuscule ; t+2 redirige (302) dès qu'il n'y a qu'une page
PHOTOS_PER_CARD = 5

# /immobilier-vente-maison/ecommoy-72220/pr-s-du-mans-belle.../334918534
_RE_AD_HREF = re.compile(
    r"/immobilier-vente-maison/(?P<loc>[^/]+)/(?P<slug>[^/]+)/(?P<id>\d{6,})$"
)
_RE_CP = re.compile(r"\b(\d{5})\b")
# Formats vus : « Prix €66.000 » (€ AVANT les chiffres), « Nouveau Prix : 369.000 € »,
# « 450.000 € » — en évitant « Taxe Foncière: 2620€ » (fallback = max des candidats).
_RE_PRIX_NOUVEAU = re.compile(r"Nouveau\s+Prix\s*:?\s*([\d][\d\s.,\xa0]{3,})\s*€", re.IGNORECASE)
_RE_PRIX_EURO_AVANT = re.compile(r"Prix\s*:?\s*€\s*([\d][\d\s.,\xa0]{3,})", re.IGNORECASE)
_RE_PRIX = re.compile(r"([\d][\d\s.,\xa0]{3,})\s*€")
_RE_SURFACE = re.compile(r"(\d{2,4})\s*m[²2]\b", re.IGNORECASE)
_RE_PIECES = re.compile(r"(\d+)\s*pi[eè]ces?", re.IGNORECASE)
_RE_CHAMBRES = re.compile(r"(\d+)\s*chambres?", re.IGNORECASE)
_RE_TERRAIN = re.compile(r"terrain[^0-9]{0,20}(\d[\d\s\xa0]{2,})\s*m[²2]", re.IGNORECASE)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_min = criteres.get("prix_min", 0)
    prix_max = criteres.get("prix_max", 0)
    surface_min = criteres.get("surface_min", 0)
    results: list[dict] = []

    async with make_client() as client:
        for dept in departements:
            slug = DEFAULT_DEPT_SLUGS.get(dept)
            if not slug:
                continue
            try:
                biens = await _scrape_dept(client, dept, slug,
                                           prix_min, prix_max, surface_min)
                results.extend(biens)
                print(f"[Vivastreet] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Vivastreet] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(client, dept, slug, prix_min, prix_max, surface_min) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set = set()

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/immobilier-vente-maison/{slug}"
        if page > 1:
            url += f"/t+{page}"
        r = await get_with_retry(client, url)
        if r is None or r.status_code != 200:
            break
        # follow_redirects : une redirection vers la page 1 (fin de liste) rend
        # les mêmes annonces → dédup seen_ids coupe et new=0 arrête la boucle.
        soup = BeautifulSoup(r.text, "html.parser")
        links = [a for a in soup.select("a[href]")
                 if _RE_AD_HREF.search(a.get("href", ""))]
        if not links:
            break
        new = 0
        for a in links:
            try:
                bien = _parse_ad(a, dept)
            except Exception:
                continue
            if not bien:
                continue
            if not keep_bien(bien, dept, seen_ids, prix_max=prix_max,
                             prix_min=prix_min, surface_min=surface_min):
                continue
            biens.append(bien)
            new += 1
        if new == 0:
            break
        await asyncio.sleep(0.5)

    return biens


def _parse_ad(a, dept: str) -> dict | None:
    href = a.get("href", "")
    m = _RE_AD_HREF.search(href)
    if not m:
        return None
    id_annonce = m.group("id")
    loc = m.group("loc")           # « ecommoy-72220 » ou « draguignan »
    url = href if href.startswith("http") else BASE_URL + href

    card = a.find_parent("li") or a.find_parent("div") or a
    text = card.get_text(" ", strip=True)

    m_cp = _RE_CP.search(loc) or _RE_CP.search(text)
    code_postal = m_cp.group(1) if m_cp else None
    ville = re.sub(r"-\d{5}$", "", loc).replace("-", " ").strip().title()

    prix = None
    m_p = _RE_PRIX_NOUVEAU.search(text) or _RE_PRIX_EURO_AVANT.search(text)
    if m_p:
        prix = parse_price_digits(m_p.group(1))
    else:
        candidats = [parse_price_digits(g) for g in _RE_PRIX.findall(text)]
        candidats = [c for c in candidats if c]
        if candidats:
            prix = max(candidats)  # écarte taxe foncière/charges, plus petites

    surface = None
    m_s = _RE_SURFACE.search(text)
    if m_s:
        val = float(m_s.group(1))
        if 20 <= val <= 1500:
            surface = val

    surface_terrain = None
    m_t = _RE_TERRAIN.search(text)
    if m_t:
        try:
            surface_terrain = float(re.sub(r"[\s\xa0]", "", m_t.group(1)))
        except ValueError:
            pass

    m_pi = _RE_PIECES.search(text)
    pieces = int(m_pi.group(1)) if m_pi else None
    m_ch = _RE_CHAMBRES.search(text)
    chambres = int(m_ch.group(1)) if m_ch else None
    dpe = parse_str_upper(r"DPE\s*:?\s*([A-G])\b", text)

    # Titre : slug de l'URL nettoyé (le DOM Tailwind n'a pas de balise titre fiable)
    titre = m.group("slug").replace("-", " ").strip().capitalize()

    photos = []
    for img in card.select("img"):
        src = img.get("data-src") or img.get("src") or ""
        if src.startswith("http") and "vivastreet" in src:
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "vivastreet",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": "maison",
        "description": text[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": None,  # majoritairement DIRECT PARTICULIER
    }


if __name__ == "__main__":
    standalone_main(search, "Vivastreet")

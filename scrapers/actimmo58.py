"""scrapers/actimmo58.py — Actimmo 58 (agence indépendante Nevers / Nièvre, depuis 1978)

Méthode : scrape_simple (httpx) — SSR HTML, moteur LBI (staticlbi.com).

Couverture : agence mono-secteur. Implantée sur la Nièvre (58 : Nevers,
             Prémery…) + un secteur « Berry 18 » (Cher). AUCUN autre département.
             Le site n'expose PAS de slug département en URL : la recherche se
             fait via un formulaire POST /recherche/ (secteurs = agences, pas
             des codes dept). On scrape donc TOUT l'inventaire puis on
             post-filtre sur code_postal[:2].

URL pattern :
  - Recherche : POST /recherche/  avec data[Search][idtype][]=1 (Maison), 5
                (Terrain)… ; offredem=0 (vente). La réponse est la page 1.
  - Pagination: GET /recherche/{N} dans la MÊME session (le critère est gardé
                en session côté serveur).
  - Détail    : /{id}-{slug}.html

Listing (cartes) :  ul.listingUL > li[onclick="location.href='/{id}-slug.html'"]
  - article.panelBien
  - Titre : h1[itemprop=name]
  - h2[itemprop=description] : "Maison  27.64 m² -  2 Pièces -  Nevers"
        (PAS de code postal dans la liste → ville seule)
  - Prix  : .prix span[itemprop=price][content]   ("23000")
  - Réf   : .ref (itemprop=productID)  →  "Ref: 7189"
  - Photos: img.mainImgLst3[src] + img.thumbslisting[src]  (// → https:)

Code postal : absent de la liste. Présent sur la page détail dans
  h1/h2 sous la forme "Ville (58350)". On récupère donc le CP en visitant la
  page détail des seuls biens candidats (et uniquement si un département cible
  recoupe la zone de l'agence — voir COVERED_DEPTS — pour éviter ~70 requêtes
  inutiles quand la zone demandée ne contient ni 58 ni 18).

Post-filtre dept STRICT : bien["code_postal"][:2] in departements → 0 fuite.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.actimmo58.fr"
MAX_PAGES = 12
PHOTOS_PER_CARD = 10
DETAIL_CONCURRENCY = 4

# Départements réellement couverts par l'agence (Nièvre + Berry/Cher).
# Sert d'optimisation : si la zone demandée ne recoupe pas ces depts, inutile
# d'aller chercher le code postal sur chaque page détail.
COVERED_DEPTS = {"58", "18"}

# Types de bien LBI à interroger (Maison, Terrain à bâtir, Immeuble…) — on garde
# l'orientation "maison / propriété" du projet.
SEARCH_IDTYPES = ["1", "5", "43", "21"]  # Maison, Terrain, Terrain à bâtir, Immeuble


_EXCLUDE_TYPE = re.compile(
    r"appartement|local|commerce|garage|parking|bureau|fonds", re.IGNORECASE
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    # Optimisation : l'agence ne couvre que 58/18. Si aucun département cible ne
    # recoupe cette zone, il n'y a rien à trouver — on évite ~70 requêtes détail.
    if not (set(departements) & COVERED_DEPTS):
        print(
            f"[Actimmo58] Zone demandée {departements} hors couverture "
            f"{sorted(COVERED_DEPTS)} → 0 annonce (skip)."
        )
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    # follow_redirects=False : le POST /recherche/ répond 302 (httpx 0.28 plante
    # si on lui demande de suivre la redirection d'un POST). On pose donc le
    # critère en session via le POST, puis on récupère les pages en GET.
    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=False, timeout=25
    ) as client:
        # NB : httpx 0.28 + AsyncClient plante sur un POST dont `data` est une
        # liste de tuples ; on passe donc un dict (valeurs multiples = liste).
        post_data = {
            "data[Search][offredem]": "0",
            "data[Search][idtype][]": list(SEARCH_IDTYPES),
        }

        # Petit retry : le POST initial peut échouer sur un aléa réseau.
        posted = False
        for attempt in range(3):
            try:
                await client.post(f"{BASE_URL}/recherche/", data=post_data)
                posted = True
                break
            except Exception as e:
                print(f"[Actimmo58] POST recherche tentative {attempt + 1} : {e}")
                await asyncio.sleep(1.5 * (attempt + 1))
        if not posted:
            return []

        try:
            r1 = await client.get(f"{BASE_URL}/recherche/")
        except Exception as e:
            print(f"[Actimmo58] Erreur GET recherche : {e}")
            return []

        cards = _extract_cards(r1.text)
        page = 2
        while page <= MAX_PAGES and not _no_more(cards):
            for card in cards:
                bien = _parse_card(card)
                if not bien or bien["id_annonce"] in seen_ids:
                    continue
                seen_ids.add(bien["id_annonce"])
                results.append(bien)
            await asyncio.sleep(0.5)
            try:
                rp = await client.get(f"{BASE_URL}/recherche/{page}")
            except Exception:
                break
            if rp.status_code != 200:
                break
            cards = _extract_cards(rp.text)
            page += 1

        print(f"[Actimmo58] {len(results)} annonces brutes récupérées.")

        # Résolution du code postal via les pages détail (CP absent de la liste).
        await _enrich_code_postaux(client, results)

    # Filtrage final : dept strict + bornes prix / surface.
    out: list[dict] = []
    vus_hors = set()
    for b in results:
        cp = b.get("code_postal") or ""
        if not cp:
            continue  # sans CP on ne peut pas garantir le dept → on écarte
        dept = cp[:2]
        if dept not in departements:
            vus_hors.add(dept)
            continue
        b["departement"] = dept
        p = b.get("prix") or 0
        s = b.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue
        out.append(b)

    if vus_hors:
        print(f"[Actimmo58] Écartés hors zone (depts {sorted(vus_hors)}).")
    print(f"[Actimmo58] {len(out)} annonces retenues pour {departements}.")
    return out


# ── Extraction liste ──────────────────────────────────────────────────────────

def _extract_cards(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    return soup.select("ul.listingUL li article.panelBien") or soup.select(
        "article.panelBien"
    )


def _no_more(cards: list) -> bool:
    return not cards


def _parse_card(card) -> dict | None:
    # URL détail : sur le <li onclick="location.href='/ID-slug.html'"> parent
    href = ""
    li = card.find_parent("li")
    if li and li.has_attr("onclick"):
        m = re.search(r"location\.href='([^']+)'", li["onclick"])
        if m:
            href = m.group(1)
    if not href:
        a = card.find("a", href=re.compile(r"/\d+-.*\.html"))
        if a:
            href = a["href"]
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    m_id = re.search(r"/(\d+)-", href)
    slug_id = m_id.group(1) if m_id else ""

    # Référence
    ref_el = card.select_one(".ref")
    ref = ""
    if ref_el:
        mref = re.search(r"Ref\s*:\s*([A-Za-z0-9]+)", ref_el.get_text(" ", strip=True))
        if mref:
            ref = mref.group(1)
    id_annonce = ref or slug_id or url

    # Titre
    title_el = card.select_one("h1[itemprop=name]") or card.select_one("h1")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    # h2 : "Maison  27.64 m² -  2 Pièces -  Nevers"
    h2 = card.select_one("h2[itemprop=description]") or card.select_one("h2")
    h2_txt = h2.get_text(" ", strip=True) if h2 else ""
    type_bien, surface, pieces, ville = _parse_h2(h2_txt)

    if _EXCLUDE_TYPE.search(type_bien):
        return None

    # Description courte
    p_el = card.select_one("header p") or card.find("p")
    description = p_el.get_text(" ", strip=True) if p_el else ""

    # Prix
    price_el = card.select_one("span[itemprop=price]")
    prix = None
    if price_el:
        prix = _parse_price(price_el.get("content") or price_el.get_text())

    # Photos
    photos = []
    main = card.select_one("img.mainImgLst3")
    if main and main.get("src"):
        photos.append(_abs_img(main["src"]))
    for img in card.select("img.thumbslisting"):
        src = img.get("src")
        if src:
            photos.append(_abs_img(src))
    photos = [p for p in dict.fromkeys(photos) if p][:PHOTOS_PER_CARD]

    if not titre:
        titre = f"{type_bien} {ville}".strip() or "Bien"

    return {
        "source": "actimmo58",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": (type_bien or "maison").lower(),
        "description": description[:1200],
        "departement": None,  # rempli après résolution CP
        "ville": ville[:80],
        "code_postal": None,  # résolu via page détail
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Actimmo 58",
    }


def _parse_h2(text: str) -> tuple[str, float | None, int | None, str]:
    """'Maison  27.64 m² -  2 Pièces -  Nevers' → (type, surface, pieces, ville)."""
    type_bien = ""
    surface = None
    pieces = None
    ville = ""
    if not text:
        return type_bien, surface, pieces, ville

    m_type = re.match(r"\s*([A-Za-zÀ-ÿ' ]+?)\s+\d", text)
    if m_type:
        type_bien = m_type.group(1).strip()

    m_surf = re.search(r"([\d]+(?:[.,]\d+)?)\s*m²", text)
    if m_surf:
        try:
            f = float(m_surf.group(1).replace(",", "."))
            if 5 <= f <= 5000:
                surface = f
        except ValueError:
            pass

    m_p = re.search(r"(\d+)\s*Pi[eè]ce", text, re.IGNORECASE)
    if m_p:
        pieces = int(m_p.group(1))

    # Ville = dernier segment après le dernier " - "
    segs = [s.strip() for s in text.split("-") if s.strip()]
    if segs:
        last = segs[-1]
        # retire éventuel "(CP)" résiduel
        last = re.sub(r"\(\d{5}\)", "", last).strip()
        # évite de prendre "X Pièces" ou "Y m²" comme ville
        if not re.search(r"m²|Pi[eè]ce", last, re.IGNORECASE):
            ville = last
    return type_bien, surface, pieces, ville


# ── Enrichissement code postal (page détail) ───────────────────────────────────

async def _enrich_code_postaux(client: httpx.AsyncClient, biens: list[dict]) -> None:
    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def one(b: dict):
        async with sem:
            try:
                r = await client.get(b["url"])
            except Exception:
                return
            if r.status_code != 200:
                return
            ville, cp = _parse_detail_loc(r.text)
            if cp:
                b["code_postal"] = cp
            if ville and not b.get("ville"):
                b["ville"] = ville[:80]
            await asyncio.sleep(0.1)

    await asyncio.gather(*(one(b) for b in biens))


def _parse_detail_loc(html: str) -> tuple[str, str]:
    """Page détail : h1/h2 contiennent 'Ville (58350)'."""
    soup = BeautifulSoup(html, "html.parser")
    for el in soup.find_all(string=re.compile(r"\(\d{5}\)")):
        m = re.search(r"([A-Za-zÀ-ÿ'\- ]+?)\s*\((\d{5})\)", el)
        if m:
            return m.group(1).strip(), m.group(2)
    # repli : tout CP dans le texte
    m = re.search(r"\b(\d{5})\b", soup.get_text())
    return "", m.group(1) if m else ""


# ── Helpers ─────────────────────────────────────────────────────────────────

def _abs_img(src: str) -> str:
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("http"):
        return src
    return BASE_URL + src


def _parse_price(text: str) -> float | None:
    if text is None:
        return None
    cleaned = re.sub(r"[^\d]", "", str(text))
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
    print(f"\nTotal Actimmo 58: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

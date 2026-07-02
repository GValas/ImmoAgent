"""scrapers/nicole_joubert.py — Agence Nicole Joubert (réseau Angers / Maine-et-Loire)

Méthode : scrape_simple (httpx) — SSR HTML (template "jalik", site SSR pur).

Réseau de 14 agences toutes situées en Maine-et-Loire (49) — inventaire MONO-
DÉPARTEMENTAL (49 uniquement, vérifié : 100 % des codes postaux des 294 cartes
listées sont 49xxx). Donc :
  - si 49 ∈ departements cibles → on scrape.
  - sinon → on ne scrape rien (court-circuit, 0 requête).

Listing (pas de filtre dept serveur, inutile car 49-only) :
  https://www.nicole-joubert.fr/biens-a-la-vente-maisons-w{N}.html        (maisons)
  https://www.nicole-joubert.fr/biens-a-la-vente-autres-biens-w{N}.html   (propriétés/longères/divers)
  Pagination réelle via -w{N}.html (12 cartes/page maisons, 9/page autres).
  Au-delà de la dernière page, le site renvoie la dernière page existante
  → détection de fin = aucune nouvelle référence sur la page.

Cartes : div.ann
  - lien/titre : a[href^="details-"]  (titre "MAISON A VENDRE MOZE SUR LOUET 49610 …")
  - référence  : .reference span:last-child
  - prix       : .prix  ("369 250 €")
  - desc       : .ann-desc
  - photos     : img[data-src] (vignette medium + survol big)
  Ville + CP : extraits du TITRE (le CP "49xxx" y figure ~90 % du temps ;
               sinon dept forcé à "49" — réseau mono-départemental confirmé).

Détail (enrichissement surface/pièces/terrain/DPE — absents des cartes) :
  Table de critères : "Nombre de pièces | 8 | Surface Habitable | 158 m² |
  Nombre de chambres | 4 | … | Surface terrain | 2500 m² | Note DPE | D".
  Fetché en httpx concurrence limitée, sur les seules cartes survivantes.

POST-FILTRE : code_postal[:2] (ou dept "49" forcé) ∈ departements cibles.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.nicole-joubert.fr"
DEPT = "49"  # réseau mono-départemental (Maine-et-Loire)
SECTIONS = ["maisons", "autres-biens"]
MAX_PAGES = 15
PHOTOS_PER_CARD = 6
DETAIL_CONCURRENCY = 6


# Mots-clés titre → on exclut explicitement les biens non "maison/propriété"
_EXCLUDE = re.compile(
    r"appartement|studio|\bterrain\b|garage|parking|local commercial|"
    r"bureau|fonds de commerce|immeuble",
    re.IGNORECASE,
)
# NB : la détection de type se fait sur le SEGMENT descriptif (après le CP),
# pas sur la portion ville — sinon "CHATEAUneuf-sur-Sarthe" matcherait "château".
_TYPE_MAP = [
    (re.compile(r"\bmanoir", re.IGNORECASE), "manoir"),
    (re.compile(r"\bch[âa]teau\b", re.IGNORECASE), "château"),
    (re.compile(r"\blong[èe]re", re.IGNORECASE), "longère"),
    (re.compile(r"propri[ée]t[ée]|demeure", re.IGNORECASE), "propriété"),
    (re.compile(r"\bvilla\b", re.IGNORECASE), "villa"),
    (re.compile(r"\bferme\b|corps de ferme", re.IGNORECASE), "ferme"),
]


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    # Réseau 49-only : si 49 hors cible, rien à faire.
    if departements and DEPT not in departements:
        print(f"[NicoleJoubert] Dept {DEPT} hors cible → skip")
        return []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        cards = await _fetch_all_cards(client)

        biens: list[dict] = []
        seen: set[str] = set()
        for c in cards:
            bien = _parse_card(c)
            if not bien:
                continue
            # POST-FILTRE département (CP[:2] sinon dept 49 forcé)
            dept = (bien.get("code_postal") or "")[:2] or DEPT
            if departements and dept not in departements:
                continue
            bien["departement"] = dept
            aid = bien.get("id_annonce") or bien.get("url")
            if aid in seen:
                continue
            seen.add(aid)
            biens.append(bien)

        # Enrichissement détail (surface / pièces / terrain / DPE)
        await _enrich_details(client, biens)

    # Filtres prix / surface (après enrichissement)
    results: list[dict] = []
    for b in biens:
        p = b.get("prix") or 0
        s = b.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue
        results.append(b)

    print(f"[NicoleJoubert] Dept {DEPT}: {len(results)} annonces")
    return results


async def _fetch_all_cards(client: httpx.AsyncClient) -> list:
    cards = []
    seen_refs: set[str] = set()
    for section in SECTIONS:
        for w in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/biens-a-la-vente-{section}-w{w}.html"
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    break
            except Exception as e:
                print(f"[NicoleJoubert] Erreur {section} w{w}: {e}")
                break

            page_cards = BeautifulSoup(r.text, "html.parser").select("div.ann")
            if not page_cards:
                break

            new = 0
            for c in page_cards:
                ref = _card_ref(c)
                key = ref or _card_href(c)
                if key in seen_refs:
                    continue
                seen_refs.add(key)
                cards.append(c)
                new += 1

            # Au-delà de la dernière page, le site répète la dernière → fin.
            if new == 0:
                break
            await asyncio.sleep(0.4)

    return cards


def _card_ref(card) -> str:
    el = card.select_one(".reference span:last-child")
    return el.get_text(strip=True) if el else ""


def _card_href(card) -> str:
    a = card.select_one('a[href*="details-"]')
    return a.get("href", "") if a else ""


def _parse_card(card) -> dict | None:
    a = card.select_one('a[href*="details-"]')
    if not a or not a.get("href"):
        return None
    href = a["href"].strip()
    url = href if href.startswith("http") else f"{BASE_URL}/{href}"
    titre = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip(" .")

    if _EXCLUDE.search(titre):
        return None

    # Ville + CP depuis le titre : "MAISON A VENDRE MOZE SUR LOUET 49610 ENTIEREMENT…"
    ville, code_postal, descr_seg = _parse_loc_from_title(titre)

    # Type déterminé sur le SEGMENT DESCRIPTIF (après ville/CP), sinon "maison"
    type_bien = "maison"
    for rx, label in _TYPE_MAP:
        if rx.search(descr_seg):
            type_bien = label
            break

    ref = _card_ref(card)
    # id numérique du slug : …-3030.html
    m_id = re.search(r"-(\d+)\.html$", href)
    id_num = m_id.group(1) if m_id else ""
    id_annonce = ref or id_num or url

    prix_el = card.select_one(".prix")
    prix = _parse_num(prix_el.get_text(" ", strip=True)) if prix_el else None

    desc_el = card.select_one(".ann-desc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    photos = []
    for img in card.select("img[data-src]"):
        src = img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif not src.startswith("http"):
                src = f"{BASE_URL}/{src.lstrip('/')}"
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "nicole_joubert",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": (code_postal or DEPT)[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": None,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "dpe": None,
        "photos": photos,
        "agence": "Nicole Joubert",
    }


async def _enrich_details(client: httpx.AsyncClient, biens: list[dict]) -> None:
    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def one(b: dict):
        async with sem:
            try:
                r = await client.get(b["url"])
                if r.status_code != 200:
                    return
                _parse_detail(r.text, b)
            except Exception:
                return

    await asyncio.gather(*(one(b) for b in biens))


def _parse_detail(html: str, b: dict) -> None:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table")
    if not table:
        return
    # Table = paires label | valeur (cellules en flux)
    cells = [c.get_text(" ", strip=True) for c in table.select("td, th")]
    kv: dict[str, str] = {}
    for i in range(0, len(cells) - 1, 2):
        kv[cells[i].lower()] = cells[i + 1]

    def g(*keys):
        for k in keys:
            for label, val in kv.items():
                if all(t in label for t in k):
                    return val
        return None

    pieces = g(["nombre", "pièce"], ["nombre", "piece"])
    if pieces:
        m = re.search(r"\d+", pieces)
        if m:
            b["pieces"] = int(m.group())

    chambres = g(["chambre"])
    if chambres:
        m = re.search(r"\d+", chambres)
        if m:
            b["chambres"] = int(m.group())

    surf = g(["surface", "habitable"])
    if surf:
        b["surface"] = _parse_num(surf)

    terr = g(["surface", "terrain"])
    if terr:
        b["surface_terrain"] = _parse_num(terr)

    dpe = g(["note dpe"], ["dpe"])
    if dpe:
        m = re.search(r"\b([A-G])\b", dpe.upper())
        if m:
            b["dpe"] = m.group(1)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_loc_from_title(titre: str) -> tuple[str, str, str]:
    """'MAISON A VENDRE MOZE SUR LOUET 49610 ENTIEREMENT…'
        → ('Moze Sur Louet', '49610', 'ENTIEREMENT…')

    Renvoie (ville, code_postal, segment_descriptif).
    Ville = ce qui suit 'A VENDRE' jusqu'au CP. Le segment descriptif (après le CP)
    sert à déterminer le type de bien sans polluer la ville.
    """
    cp = ""
    m_cp = re.search(r"\b(49\d{3})\b", titre)
    if not m_cp:
        m_cp = re.search(r"\b(\d{5})\b", titre)
    if m_cp:
        cp = m_cp.group(1)

    m = re.search(r"\bA\s+VENDRE\s+(.+)", titre, re.IGNORECASE)
    rest = m.group(1) if m else titre

    descr_seg = ""
    if cp:
        before, _, after = rest.partition(cp)
        ville_raw = before
        descr_seg = after
    else:
        # Pas de CP : la ville est en MAJUSCULES en tête ; on coupe au 1er mot
        # contenant une minuscule (= début du descriptif en casse mixte).
        words = rest.split()
        keep, rest_words = [], []
        for i, wd in enumerate(words):
            if i > 0 and re.search(r"[a-zàâçéèêëîïôûùüÿ]", wd):
                rest_words = words[i:]
                break
            keep.append(wd)
        ville_raw = " ".join(keep)
        descr_seg = " ".join(rest_words)

    ville = re.sub(r"\s+", " ", ville_raw).strip(" .-")
    if ville.isupper():
        ville = ville.title()
    return ville[:80], cp, descr_seg.strip()


def _parse_num(text: str) -> float | None:
    """'369 250  €' / '158 m²' / '2500 m²' → float."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d,\.]", "", text.replace("\xa0", "").replace(" ", ""))
    cleaned = cleaned.replace(",", ".")
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


# ── CLI standalone ──────────────────────────────────────────────────────────────

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
    print(f"\nTotal Nicole Joubert: {len(biens)} annonces")
    from collections import Counter

    cible = {str(d).zfill(2) for d in criteres.departements}
    dist = Counter(b["departement"] or "??" for b in biens)
    print(f"Répartition département : {dict(dist)}")
    # Fuite = département (champ de filtre) hors cible ; le CP peut être vide
    # (titre sans CP) → dept 49 forcé, ce qui est légitime (réseau 49-only).
    leaks = [b for b in biens if b["departement"] not in cible]
    print(f"FUITES hors-départements : {len(leaks)}")
    for b in leaks[:5]:
        print(f"   LEAK [{b['departement']}|{b['code_postal']}] {b['titre'][:50]}")
    sans_cp = sum(1 for b in biens if not b["code_postal"])
    print(f"(dont {sans_cp} sans CP dans le titre → dept 49 forcé)")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal'] or '?????'}] {b['titre'][:50]}"
            f" — {b['prix']}€ — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

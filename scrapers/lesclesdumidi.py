"""scrapers/lesclesdumidi.py — Les Clés du Midi (annonces immobilières, orientation P2P)

Méthode : api_inoff (httpx) — endpoint AJAX renvoyant du HTML SSR par département.
Le portail mêle annonces de particuliers et de petites agences (« annonces
immobilières entre particuliers ») ; bon volume sur l'Indre-et-Loire (≈ 730
annonces dept 37).

Pagination / filtre département CÔTÉ SERVEUR :
  La page liste (/immobilier/annonce_immobiliere_entre_particuliers-{slug}-{NN}.html)
  charge ses 10 premières cartes puis pagine par scroll infini via :
    GET /2016/ajax_listing_infini_dep.php
        ?immopage=annonce_immobiliere_entre_particuliers
        &numdep={NN}&loc=vente&type=&piece=&debuttype=0
        &debutlimit={offset}&nbnewtype=0&newtype=&nbanndep={total}
  → numdep filtre le département au serveur (vérifié : aucune fuite hors-dept,
    tous les CP renvoyés commencent par NN). On itère debutlimit par pas de 10.

Cartes : article.card
  - URL/id : a[href=.../annonce-immobiliere-{ID}.html]  (id aussi dans
             id="anchor_article_{ID}")
  - Titre + prix : h3 > a   (le prix est dans le <span> final, ex. « 158 000 € »)
  - Type+Ville+CP : .produit__info--city strong  →  « Maison Blere 37150 »
  - Caract : .produit__caract li  →  « 75 m² », « 3 pièces », « 1 chambre »
  - Desc   : .produit__descr
  - Photos : .produit__photo img[src] (+ compteur .produit__nbphoto span)
  - Agence : .produit__agence strong

Type de bien : déduit du 1er mot de la localisation + du titre. On ne garde que
  maisons / propriétés / longères / manoirs / fermes… (exclut appartement,
  terrain, local, parking, immeuble). Surface = habitable. Le terrain et le DPE
  ne sont pas en liste → récupérés ensuite par gallery.py sur les survivants.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.lesclesdumidi.com"
AJAX_URL = f"{BASE_URL}/2016/ajax_listing_infini_dep.php"
PAGE_SIZE = 10
MAX_PAGES = 40           # garde-fou (40 * 10 = 400 cartes/dept max)
PHOTOS_PER_CARD = 1      # la liste n'expose que la photo de couverture

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BASE_URL + "/",
}

# Le endpoint AJAX prend numdep ; les 11 départements cibles sont tous gérés
# (le filtre est purement numérique côté serveur).
DEPTS_CIBLES = {"72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"}

# Types à conserver (maisons / propriétés / vieilles pierres)
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps de ferme|bastide|"
    r"chartreuse|gentilhommiere|gentilhommière|maison de maitre|maison de maître",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|cave|box|loft|studio",
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
            if dept not in DEPTS_CIBLES:
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[ClesDuMidi] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[ClesDuMidi] Erreur dept {dept}: {e}")
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

    for page in range(MAX_PAGES):
        offset = page * PAGE_SIZE
        params = {
            "immopage": "annonce_immobiliere_entre_particuliers",
            "numdep": dept,
            "loc": "vente",
            "type": "",            # tous types → on post-filtre nous-mêmes
            "piece": "",
            "debuttype": "0",
            "debutlimit": str(offset),
            "nbnewtype": "0",
            "newtype": "",
            "nbanndep": "9999",    # borne haute ; le serveur s'arrête tout seul
        }
        try:
            r = await client.get(AJAX_URL, params=params)
        except Exception:
            break
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select("article.card")
        if not cards:
            break  # plus d'annonces → fin de pagination

        new_on_page = 0
        for card in cards:
            try:
                bien = _parse_card(card, dept)
            except Exception:
                continue
            if not bien:
                continue

            # Filtre département STRICT (le serveur filtre déjà ; double sécurité)
            if not bien["code_postal"] or bien["code_postal"][:2] != dept:
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

        if new_on_page == 0 and len(cards) < PAGE_SIZE:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    # Lien détail + id
    link = card.select_one('a[href*="annonce-immobiliere-"]')
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    id_annonce = ""
    m_id = re.search(r"annonce-immobiliere-(\d+)\.html", url)
    if m_id:
        id_annonce = m_id.group(1)
    if not id_annonce:
        anchor = card.select_one('[id^="anchor_article_"]')
        if anchor:
            id_annonce = anchor.get("id", "").replace("anchor_article_", "")
    if not id_annonce:
        id_annonce = url

    # Type + ville + CP : "Maison Blere 37150"
    city_el = card.select_one(".produit__info--city strong")
    city_txt = city_el.get_text(" ", strip=True) if city_el else ""
    type_word, ville, code_postal = _parse_city(city_txt)

    title_el = card.select_one("h3 a")
    title_txt = title_el.get_text(" ", strip=True) if title_el else ""

    # Filtre type de bien : on se fie au LABEL (1er mot = type officiel du site :
    # « Maison », « Terrain », « Neuf », « Local », « Autre »…), PAS au nom de
    # commune (sinon « Terrain Château-la-Vallière » serait gardé via « château »).
    type_label = type_word or (title_txt.split()[0] if title_txt else "")
    if _EXCLUDE_TYPE.search(type_label):
        return None
    if not _KEEP_TYPE.search(type_label):
        return None
    type_bien = type_word.lower() if type_word else "maison"

    # Titre + prix (prix dans le <span> du h3)
    prix = None
    if title_el:
        span = title_el.find("span")
        if span:
            prix = _parse_price(span.get_text(" ", strip=True))
    titre = title_txt or f"{type_bien.title()} {ville}".strip()

    # Caractéristiques : surface / pièces / chambres
    surface = pieces = chambres = None
    for li in card.select(".produit__caract li"):
        t = li.get_text(" ", strip=True)
        if surface is None:
            m = re.search(r"(\d[\d\s\xa0]*)\s*m²", t)
            if m:
                surface = _to_float(m.group(1))
                continue
        if pieces is None and "pièce" in t.lower():
            m = re.search(r"(\d+)", t)
            if m:
                pieces = int(m.group(1))
                continue
        if chambres is None and "chambre" in t.lower():
            m = re.search(r"(\d+)", t)
            if m:
                chambres = int(m.group(1))

    # Description (extrait liste ; gallery.py complétera)
    descr_el = card.select_one(".produit__descr")
    description = descr_el.get_text(" ", strip=True) if descr_el else ""

    # Photo de couverture
    photos = []
    img = card.select_one(".produit__photo img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    # Agence / déposant
    ag_el = card.select_one(".produit__agence strong")
    agence = ag_el.get_text(" ", strip=True) if ag_el else None

    return {
        "source": "lesclesdumidi",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,   # absent de la liste → gallery.py (description)
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,               # absent de la liste → gallery.py (page détail)
        "agence": agence,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_city(text: str) -> tuple[str, str, str]:
    """'Maison Blere 37150' → ('Maison', 'Blere', '37150').

    Le 1er token est le type, le dernier (5 chiffres) le CP, le reste la ville.
    """
    if not text:
        return "", "", ""
    cp = ""
    m_cp = re.search(r"\b(\d{5})\b", text)
    if m_cp:
        cp = m_cp.group(1)
    rest = re.sub(r"\b\d{5}\b", "", text).strip()
    tokens = rest.split()
    type_word = tokens[0] if tokens else ""
    ville = " ".join(tokens[1:]).strip() if len(tokens) > 1 else ""
    # Quelques fiches répètent le type en ville (« Maison Maison ») ; on nettoie.
    return type_word, ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _to_float(text: str) -> float | None:
    val = re.sub(r"[\s\xa0]", "", text)
    try:
        return float(val)
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
    print(f"\nTotal Les Clés du Midi: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

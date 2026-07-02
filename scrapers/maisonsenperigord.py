"""scrapers/maisonsenperigord.py — Maisons en Périgord (agence Montignac-Lascaux)

Site : https://maisonsenperigord.net
Méthode : scrape_simple (httpx) — SSR WordPress (thème mep23), serveur Apache.
URL liste : /nos-biens-immobiliers  → toutes les annonces sont rendues côté serveur
            sur une seule page (pas de pagination, ~20-25 biens).

Agence MONO-DÉPARTEMENT : toute l'agence couvre le Périgord Noir / Dordogne (24).
  - L'adresse de l'agence (24290 MONTIGNAC) est le seul code postal présent sur les
    pages ; les annonces ne portent PAS de commune/CP propre (localisation en prose
    dans le titre/description : "à 6 km de Sarlat", "Les Eyzies"…).
  - Stratégie filtre département : département fixé à "24". Si "24" n'est pas dans
    `criteres["departements"]`, le scraper renvoie une liste vide → 0 fuite garantie.
  - `code_postal` laissé à None (inconnu au niveau du bien) ; `departement = "24"`.

Cartes (page liste) : div.bg-white contenant a[href*="/maison-a-vendre/"]
  - URL    : a.block[href]  → /maison-a-vendre/{slug}-{id}.html
  - Titre  : h2 (description géographique en majuscules)
  - Réf    : div.text-gold  → "DEP1021 - Maison Ancienne" (réf + sous-type)
  - Prix   : div.bg-moledark → "140 400 € HAI"
  - Texte  : div.mb-6 (début de description)
  - Stats  : div.bg-neutral-200 span → ["83 m 2", "4 pièce(s)", "2 chambre(s)", "1 salle(s) de bain"]
  - Photos : img[src] (CDN label-pierres)

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://maisonsenperigord.net"
LIST_URL = f"{BASE_URL}/nos-biens-immobiliers"
DEPT = "24"  # Dordogne — agence mono-département
PHOTOS_PER_CARD = 10


_TYPE_RE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|long[èe]re|manoir|ch[âa]teau|moulin|"
    r"demeure|domaine|mas|gite|gîte|grange",
    re.IGNORECASE,
)
_EXCLUDE_RE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    # Agence mono-département : rien à faire si la Dordogne n'est pas demandée.
    if DEPT not in departements:
        print(f"[MaisonsEnPerigord] Dept {DEPT} hors zone demandée → 0 annonce")
        return []

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        try:
            r = await client.get(LIST_URL)
            if r.status_code != 200:
                print(f"[MaisonsEnPerigord] HTTP {r.status_code} sur la liste")
                return []
            cards = _select_cards(r.text)
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # Sécurité département (le bien est forcément en 24 ; on le re-vérifie
                # explicitement pour rester aligné avec la convention "0 fuite").
                if bien["departement"] != DEPT or DEPT not in departements:
                    continue

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                results.append(bien)
            print(f"[MaisonsEnPerigord] Dept {DEPT}: {len(results)} annonces")
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"[MaisonsEnPerigord] Erreur: {e}")

    return results


def _select_cards(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    cards = [
        c
        for c in soup.select("div.bg-white")
        if c.select_one('a[href*="/maison-a-vendre/"]')
    ]
    # Déduplication par URL de détail (la page répète parfois les conteneurs).
    seen: set[str] = set()
    uniq: list = []
    for c in cards:
        a = c.select_one('a[href*="/maison-a-vendre/"]')
        href = a.get("href", "") if a else ""
        if not href or href in seen:
            continue
        seen.add(href)
        uniq.append(c)
    return uniq


def _parse_card(card) -> dict | None:
    link = card.select_one('a[href*="/maison-a-vendre/"]')
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce : id numérique en fin d'URL (…-6504539.html)
    m_id = re.search(r"-(\d+)\.html?$", href)
    id_url = m_id.group(1) if m_id else url

    # Réf + sous-type : "DEP1021 - Maison Ancienne"
    ref_el = card.select_one("div.text-gold")
    ref_txt = ref_el.get_text(" ", strip=True) if ref_el else ""
    ref = ""
    type_bien = "maison"
    if ref_txt:
        parts = ref_txt.split("-", 1)
        ref = parts[0].strip()
        if len(parts) > 1:
            sub = parts[1].strip()
            if sub:
                type_bien = sub
    id_annonce = ref or id_url

    # Filtre type : on écarte appartements/terrains/locaux si explicitement nommés
    if _EXCLUDE_RE.search(type_bien) and not _TYPE_RE.search(type_bien):
        return None

    # Titre
    h2 = card.select_one("h2")
    titre = h2.get_text(" ", strip=True) if h2 else type_bien
    titre = re.sub(r"\s+", " ", titre).strip()

    # Description (extrait sur la carte)
    desc_el = card.select_one("div.mb-6")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Prix : "140 400 € HAI"
    price_el = card.select_one("div.bg-moledark")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Stats latérales : surface / pièces / chambres / salles de bain
    stats = [s.get_text(" ", strip=True) for s in card.select("div.bg-neutral-200 span")]
    surface = pieces = chambres = None
    for st in stats:
        if surface is None:
            ms = re.search(r"(\d[\d\s\xa0]*)\s*m", st)
            if ms and "pi" not in st.lower() and "chambre" not in st.lower():
                try:
                    val = float(re.sub(r"[\s\xa0]", "", ms.group(1)))
                    if 8 <= val <= 3000:
                        surface = val
                        continue
                except ValueError:
                    pass
        if pieces is None and "pi" in st.lower():
            mp = re.search(r"(\d+)", st)
            if mp:
                pieces = int(mp.group(1))
                continue
        if chambres is None and "chambre" in st.lower():
            mc = re.search(r"(\d+)", st)
            if mc:
                chambres = int(mc.group(1))

    # Terrain : tenté dans le titre/description ("sur 3900 m²", "5700 m2 de terrain")
    surface_terrain = _parse_terrain(titre) or _parse_terrain(description)

    # Photos
    photos = []
    for img in card.select("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    # dédup en conservant l'ordre
    photos = list(dict.fromkeys(photos))[:PHOTOS_PER_CARD]

    return {
        "source": "maisonsenperigord",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": DEPT,
        "ville": None,  # commune non exposée au niveau du bien
        "code_postal": None,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Maisons en Périgord",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text.replace("HAI", ""))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_terrain(text: str) -> float | None:
    """Cherche un terrain en m² dans le texte ('sur 3900 m²', '5700 m2 de terrain')."""
    if not text:
        return None
    for m in re.finditer(r"(\d[\d\s\xa0]{1,7})\s*m(?:²|2)\b", text, re.IGNORECASE):
        try:
            val = float(re.sub(r"[\s\xa0]", "", m.group(1)))
        except ValueError:
            continue
        # Heuristique : un terrain fait > 200 m² (au-delà d'une surface habitable usuelle)
        if 200 <= val <= 5_000_000:
            return val
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
    print(f"\nTotal Maisons en Périgord: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['pieces'] or '?'}p — {b['type_bien']}"
        )

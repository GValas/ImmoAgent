"""scrapers/noovimo.py — Noovimo (réseau de mandataires Atlantique / Ouest)

Méthode : scrape_simple (httpx) — SSR Webflow CMS.
Le site est un Webflow (CSR Finsweet en surface) MAIS la liste des biens est
rendue côté serveur, 24 cartes par page, via la **pagination native Webflow** :
    /biens-a-vendre?378596e5_page=N   (page 1 = /biens-a-vendre sans query)
Chaque page renvoie 24 cartes DISTINCTES en HTML pur (pas besoin de JS).

⚠ Pas de filtre département serveur (le filtre du site est client-side Finsweet).
Inventaire NATIONAL gérable (~700 annonces, ~30 pages) → on scrape tout et on
POST-FILTRE par code_postal[:2] (champ `fs-list-field="color"` de chaque carte).

Cartes : .cms_list-item (lien a.re01[href="/biens-a-vendre/{id}-{type}-{slug}"])
  Champs fs-list-field exposés dans la carte (texte) :
    - Référence : "maison/villa" | "appartement" | "terrain"  (= type)
    - name      : titre de l'annonce
    - ville     : ville
    - color     : code postal (ex "44120")  ← utilisé pour le filtre dept
    - prix      : prix entier en € (ex "297540")
    - m²        : surface habitable (ex "105")
    - type      : type de bien (idem Référence)
    - piece     : nombre de pièces
    - terrasse/cave/piscine : "Oui" ou vide
    - date      : date de mise à jour (M/D/YYYY)
  Photo : img.re01_img[src]

On ne garde que les MAISONS/VILLAS (type "maison/villa") — appartements/terrains
exclus. Le champ `type` est fiable même quand le `name` est trompeur.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.noovimo.fr"
LISTING_PATH = "/biens-a-vendre"
PAGE_PARAM = "378596e5_page"   # param de pagination native Webflow de la liste biens
MAX_PAGES = 50                 # plafond de sécurité (~30 pages réelles)
PHOTOS_PER_CARD = 1            # 1 photo de couverture sur la liste


# Types conservés (champ `type` de la carte) : maisons/villas uniquement.
_KEEP_TYPE = re.compile(r"maison|villa|propri[eé]t[eé]|demeure|manoir|long[eè]re", re.IGNORECASE)
_EXCLUDE_TYPE = re.compile(r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau", re.IGNORECASE)


def _field(card, name: str) -> str:
    el = card.select_one(f'[fs-list-field="{name}"]')
    return el.get_text(" ", strip=True) if el else ""


def _to_int(text: str) -> int | None:
    cleaned = re.sub(r"[^\d]", "", (text or "").replace("\xa0", " "))
    try:
        return int(cleaned) if cleaned else None
    except ValueError:
        return None


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    results: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25) as client:
        empty_streak = 0
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}{LISTING_PATH}"
            params = {} if page == 1 else {PAGE_PARAM: str(page)}
            try:
                r = await client.get(url, params=params)
                if r.status_code != 200:
                    break
            except Exception as e:
                print(f"[Noovimo] Erreur page {page}: {e}")
                break

            cards = BeautifulSoup(r.text, "html.parser").select(".cms_list-item")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                bien = _parse_card(card)
                if not bien:
                    continue

                aid = bien.get("id_annonce") or bien.get("url")
                if aid in seen:
                    continue
                seen.add(aid)
                new_on_page += 1

                # POST-FILTRE département via code_postal[:2]
                cp = bien.get("code_postal") or ""
                dept = cp[:2] if len(cp) >= 2 else ""
                if departements and dept not in departements:
                    continue
                bien["departement"] = dept

                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                results.append(bien)

            # Plus aucune nouvelle annonce sur 2 pages consécutives → fin du listing
            if new_on_page == 0:
                empty_streak += 1
                if empty_streak >= 2:
                    break
            else:
                empty_streak = 0

            await asyncio.sleep(0.4)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[Noovimo] Dept {dept}: {n} annonces")

    return results


def _parse_card(card) -> dict | None:
    try:
        a = card.select_one('a.re01[href], a[href*="/biens-a-vendre/"]')
        if not a or not a.get("href"):
            return None
        href = a["href"].strip()
        # ne garder que les fiches biens (et pas un lien parasite)
        if not re.search(r"/biens-a-vendre/\d+-", href):
            return None
        url = href if href.startswith("http") else BASE_URL + href

        # type de bien (champ fiable)
        type_raw = _field(card, "type") or _field(card, "Référence")
        if _EXCLUDE_TYPE.search(type_raw) and not _KEEP_TYPE.search(type_raw):
            return None
        if not _KEEP_TYPE.search(type_raw):
            return None
        type_bien = "maison" if re.search(r"maison|villa", type_raw, re.IGNORECASE) else type_raw.lower()

        code_postal = _field(card, "color")
        m_cp = re.search(r"\d{5}", code_postal)
        code_postal = m_cp.group(0) if m_cp else ""

        ville = _field(card, "ville")
        titre = _field(card, "name") or f"{type_bien.title()} {ville}".strip()
        prix = _to_int(_field(card, "prix"))
        surface = _to_int(_field(card, "m²"))
        pieces = _to_int(_field(card, "piece"))

        # id annonce depuis le slug d'URL : /biens-a-vendre/{id}-...
        id_annonce = None
        m_id = re.search(r"/biens-a-vendre/(\d+)-", href)
        if m_id:
            id_annonce = m_id.group(1)

        # photo de couverture
        photos = []
        img = card.select_one("img.re01_img") or card.select_one("img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http") and not src.startswith("data:"):
                photos.append(src)
        photos = photos[:PHOTOS_PER_CARD]

        return {
            "source": "noovimo",
            "url": url,
            "id_annonce": id_annonce,
            "titre": titre[:150],
            "type_bien": type_bien,
            "description": None,
            "departement": (code_postal or "")[:2],
            "ville": (ville or "")[:80],
            "code_postal": code_postal,
            "surface": float(surface) if surface else None,
            "surface_terrain": None,
            "pieces": pieces,
            "chambres": None,
            "prix": float(prix) if prix else None,
            "dpe": None,
            "photos": photos,
            "agence": "Noovimo",
        }
    except Exception:
        return None


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
    print(f"\nTotal Noovimo (depts cibles): {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    # contrôle fuite
    cibles = {str(d).zfill(2) for d in criteres.departements}
    fuites = [b for b in biens if b["code_postal"][:2] not in cibles]
    print(f"FUITES hors-dept : {len(fuites)}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€ — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p — {b['type_bien']} — {b['ville']}"
        )

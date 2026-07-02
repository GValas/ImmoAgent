"""scrapers/rnc_notaires.py — Réseau Notaires & Conseils (RNC), office notarial Sarthe

Méthode : scrape_simple (httpx) — SSR HTML (plateforme GenAPI / sites-notaires.immonot.com)
Site    : https://www.rnc.notaires.fr  (SELAS Réseau Notaires & Conseils, Arnage 72)

Couverture : réseau d'études notariales de la SARTHE (72) uniquement. L'inventaire
             complet de cet office est en Sarthe → seul le département 72 sort.
             (Aucune fuite : chaque annonce a un slug d'URL « ...-sarthe-... » ;
              post-filtre strict sur le nom de département du slug + CP[:2] si présent.)

URL pattern :
  - Page liste filtrée Sarthe : /annonces-immobilieres-sarthe.html
    (= alias de /fr_FR/3/1/annonces-immobilieres.html, code office « 3 »)
  - Pagination : /fr_FR/3/{page}/annonces-immobilieres.html   (9 cartes / page)

Cartes : div.bloc-annonce-carre  (à l'intérieur, un seul <a> vers le détail)
  - URL    : a[href]  → /annonces/detail/{id}__w{ref}/key/3/vente-{type}-{dept}-{ville}.html
  - Type   : 1er .col-md-8 du bloc texte → « Vente Maison » / « Vente Appartement »
  - Prix   : .col-md-4.text-right          → « 372 000 € »
  - Ville  : .col-md-5.light-color         → « La Suze-sur-Sarthe »
  - Surface: .col-md-4 (3e ligne)          → « 200.0 m² »
  - Réf    : .text-right.light-color       → « Réf. HB-1862 »
  - Photo  : img.img-responsive[src]       → /photoProduit/...jpg

Le département est déduit du segment de département dans le slug d'URL détail
(« vente-maison-sarthe-... » → 72). Le code postal n'est pas dans la carte liste
(il est en page détail, récupéré ensuite par gallery.py) ; on conserve donc le
filtrage via le nom de département du slug, fiable car l'office est mono-département.

Pièces / chambres / terrain / CP / DPE / description complète : en page détail
(enrichis ensuite par gallery.py) — la carte liste ne les expose pas.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.rnc.notaires.fr"
OFFICE_CODE = "3"
MAX_PAGES = 10
PHOTOS_PER_CARD = 1  # la liste n'expose qu'une vignette ; galerie via gallery.py


# Nom de département (slug d'URL détail) → code département.
# L'office RNC est en Sarthe ; on garde le mapping pour un filtre robuste et lisible.
_DEPT_NAME_TO_CODE: dict[str, str] = {
    "sarthe": "72",
    "eure-et-loir": "28",
    "loiret": "45",
    "yonne": "89",
    "maine-et-loire": "49",
    "indre-et-loire": "37",
    "indre": "36",
    "cher": "18",
    "nievre": "58",
    "loir-et-cher": "41",
    "mayenne": "53",
}

# Types de bien conservés (segment de slug d'URL) : maisons / propriétés / fermes…
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"viager|location|locati",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # L'office ne couvre que la Sarthe : si 72 n'est pas demandé, rien à faire.
    if "72" not in departements:
        print("[RNC] Dept 72 non demandé — office Sarthe ignoré")
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/fr_FR/{OFFICE_CODE}/{page}/annonces-immobilieres.html"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[RNC] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(".bloc-annonce-carre")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # Post-filtre dept STRICT : dept déduit du slug doit être ciblé.
                dept = bien["departement"]
                if dept not in departements:
                    continue
                if bien["code_postal"] and bien["code_postal"][:2] != dept:
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
                results.append(bien)
                new_on_page += 1

            if new_on_page == 0 and page > 1:
                break

            await asyncio.sleep(0.6)

    print(f"[RNC] Total : {len(results)} annonces (Sarthe)")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Slug d'URL : .../vente-{type}-{dept}-{ville}.html
    slug = url.rsplit("/", 1)[-1].removesuffix(".html")
    slug_parts = slug.split("-")
    # « vente », type, puis dept (nom) — on cherche le nom de dept connu dans le slug
    type_seg = ""
    dept = ""
    # Détecte le nom de département le plus long présent dans le slug
    for name, code in _DEPT_NAME_TO_CODE.items():
        if f"-{name}-" in f"-{slug}-":
            dept = code
            # le type est ce qui précède le nom de dept (après « vente »)
            head = slug.split(f"-{name}-", 1)[0]  # ex « vente-maison »
            type_seg = head.split("-", 1)[1] if "-" in head else head
            break
    if not dept:
        return None

    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if type_seg and not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = (type_seg or "maison").replace("-", " ").strip()

    # id_annonce : segment w{ref} de l'URL, sinon réf affichée
    id_annonce = ""
    m_id = re.search(r"__w(\w+)/", href)
    if m_id:
        id_annonce = m_id.group(1)

    # Bloc texte (2e container-fluid)
    txt = card.get_text(" ", strip=True)

    # Type affiché (ex « Vente Maison ») — confirme/complète type_bien
    title_type = ""
    m_t = re.search(r"\b(Vente|Location)\s+([A-Za-zÀ-ÿ'’\- ]+?)\s+\d", txt)
    if m_t:
        title_type = m_t.group(2).strip()

    # Prix : premier « NNN NNN € »
    prix = None
    m_p = re.search(r"([\d][\d\s\xa0]*)\s*€", txt)
    if m_p:
        cleaned = re.sub(r"[\s\xa0]", "", m_p.group(1))
        if cleaned.isdigit():
            prix = float(cleaned)

    # Ville : .light-color (1ère colonne, pas le bloc honoraires)
    ville = ""
    for el in card.select(".light-color"):
        cand = el.get_text(" ", strip=True)
        if cand and "Honoraires" not in cand and "Réf" not in cand and "€" not in cand \
           and "Soit" not in cand and not cand.startswith("%"):
            ville = cand
            break

    # Surface : « NNN.N m² »
    surface = None
    m_s = re.search(r"([\d]+(?:[.,]\d+)?)\s*m²", txt)
    if m_s:
        try:
            val = float(m_s.group(1).replace(",", "."))
            if 8 <= val <= 5000:
                surface = val
        except ValueError:
            pass

    # Référence
    ref = None
    m_r = re.search(r"Réf\.?\s*([\w\-/]+)", txt)
    if m_r:
        ref = m_r.group(1)
    if not id_annonce:
        id_annonce = ref or url

    # Photo (vignette liste)
    photos = []
    img = card.select_one("img.img-responsive, img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:") and "marianne" not in src:
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    titre = f"{title_type or type_bien.title()} {ville}".strip()

    return {
        "source": "rnc_notaires",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien or "maison",
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",  # absent en liste ; récupéré en page détail (gallery.py)
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Réseau Notaires & Conseils (Sarthe)",
    }


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
    print(f"\nTotal RNC Notaires : {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

"""scrapers/notaires_fruchon_36.py — Office Notarial FRUCHON & ASSOCIÉS (Châteauroux, 36)

Office notarial FRUCHON & ASSOCIÉS, Châteauroux (Indre). Service de
négociation immobilière. Cœur d'activité : l'Indre (36) — département cible
sous-pourvu.

Méthode : scrape_simple (httpx) — SSR HTML (template Genapi/jurisme, même
famille que les autres portails *.notaires.fr en SSR).
URL pattern : /annonces-immobilieres-fruchon-associes.html
              → PAS de filtre département côté serveur (office mono-secteur).
              On récupère tout, puis post-filtre STRICT sur le département.

Pagination : ?page=N existe mais re-sert toujours la même page (stock unique,
~5 annonces). On fetch une fois, on déduplique par id et on stoppe.

Cartes : div.bloc-annonce
  - Lien détail "officiel" : a[href*="immobilier.notaires.fr/fr/annonce-immo"]
        → /vente/{type}/{ville}-{NN}/{id}  (source fiable du département + id)
  - Lien détail local     : .lire-plus a[href]  (detail-annonces-...{id}.html?token=)
  - Ville + dept : .desc-immo .titre  →  "ST MAUR (36)"
  - Type + pièces + surface : .titre-detail  →  "Maison / villa - 4 pièce(s) - 107 m²"
  - Prix  : .immo-prix  →  "84 000 €" (suivi d'un .small-info à ignorer)
  - Desc  : .desc-immo-detail
  - Photo : img.lazyload[data-src]
  - Réf interne (alt)  : "... - 36003/11807/465"

Type de bien : déduit de l'URL/segment + de .titre-detail. On ne garde que
maisons / propriétés (appartement & terrain exclus).

Le listing n'expose qu'un département (NN), pas un code postal à 5 chiffres :
on dérive le département de l'URL canonique immobilier.notaires.fr et on
post-filtre STRICTEMENT dessus (0 fuite hors-zone).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://fruchon-notaire-chateauroux.notaires.fr"
LISTING = f"{BASE_URL}/annonces-immobilieres-fruchon-associes.html"
MAX_PAGES = 6
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Départements cibles (post-filtre strict)
TARGET_DEPTS = {"72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"}

# Types de bien à conserver : maisons / propriétés...
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|"
    r"maison-de-village|fermette|grange|propriete",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"viager",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    allowed = departements & TARGET_DEPTS if departements else set(TARGET_DEPTS)

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = LISTING if page == 1 else f"{LISTING}?page={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Fruchon36] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.bloc-annonce")
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

                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                new_on_page += 1

                # Post-filtre dept STRICT (pas de filtre serveur)
                if bien["departement"] not in allowed:
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

            # Page sans aucune annonce nouvelle (re-service de la même page)
            if new_on_page == 0:
                break

            await asyncio.sleep(0.5)

    print(f"[Fruchon36] {len(results)} annonces retenues (zone cible)")
    return results


def _parse_card(card) -> dict | None:
    # Lien détail canonique immobilier.notaires.fr → /vente/{type}/{ville}-{NN}/{id}
    canon = card.find(
        "a", href=re.compile(r"immobilier\.notaires\.fr/fr/annonce-immo")
    )
    canon_href = canon.get("href", "") if canon else ""

    # Lien détail local (préféré comme url de scraping)
    local = card.select_one(".lire-plus a[href]")
    local_href = local.get("href", "") if local else ""
    if local_href:
        url = (
            local_href
            if local_href.startswith("http")
            else f"{BASE_URL}/{local_href.lstrip('/')}"
        )
    elif canon_href:
        url = canon_href
    else:
        return None

    # Département + type + id depuis l'URL canonique
    dept = ""
    type_seg = ""
    canon_id = ""
    m = re.search(
        r"/annonce-immo/vente/([^/]+)/[^/]+-(\d{2,3})/(\d+)", canon_href
    )
    if m:
        type_seg = m.group(1)
        dept = m.group(2)[:2]
        canon_id = m.group(3)

    # id de secours depuis l'URL locale (detail-...{id}.html)
    id_annonce = canon_id
    if not id_annonce:
        m_id = re.search(r"/(\d+)\.html", local_href)
        if m_id:
            id_annonce = m_id.group(1)
    if not id_annonce:
        id_annonce = url

    # Ville + dept depuis .titre  →  "ST MAUR (36)"
    titre_loc_el = card.select_one(".desc-immo .titre")
    ville = ""
    if titre_loc_el:
        loc_txt = titre_loc_el.get_text(" ", strip=True)
        m_dep = re.search(r"\((\d{2,3})\)\s*$", loc_txt)
        if m_dep and not dept:
            dept = m_dep.group(1)[:2]
        ville = re.sub(r"\s*\(\d{2,3}\)\s*$", "", loc_txt).strip()

    # Détail : "Maison / villa - 4 pièce(s) - 107 m²"
    detail_el = card.select_one(".titre-detail")
    detail_txt = detail_el.get_text(" ", strip=True) if detail_el else ""
    detail_txt = re.sub(r"\s+", " ", detail_txt).strip()

    # Type de bien (segment d'URL prioritaire, sinon texte détail)
    type_src = type_seg or detail_txt
    if _EXCLUDE_TYPE.search(type_src) and not _KEEP_TYPE.search(type_src):
        return None
    if not _KEEP_TYPE.search(type_src):
        return None
    if type_seg:
        type_bien = type_seg.replace("-", " ").strip()
    else:
        type_bien = detail_txt.split("-")[0].strip() or "maison"

    # Pièces
    pieces = None
    m_p = re.search(r"(\d+)\s*pi[eè]ce", detail_txt, re.IGNORECASE)
    if m_p:
        pieces = int(m_p.group(1))

    # Surface habitable
    surface = None
    m_s = re.search(r"(\d[\d\s\xa0]*)\s*m²", detail_txt)
    if m_s:
        try:
            surface = float(re.sub(r"[\s\xa0]", "", m_s.group(1)))
        except ValueError:
            surface = None

    # Titre
    titre = f"{type_bien.title()} {ville}".strip()
    if detail_txt:
        titre = f"{detail_txt} - {ville}".strip(" -")

    # Description
    desc_el = card.select_one(".desc-immo-detail")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Prix : .immo-prix — ne garder que le montant de tête (avant .small-info)
    prix = None
    prix_el = card.select_one(".immo-prix")
    if prix_el:
        # Texte direct du noeud (avant les enfants comme .small-info)
        head = ""
        for child in prix_el.children:
            if getattr(child, "name", None) is None:  # NavigableString
                head += str(child)
            else:
                break
        prix = _parse_price(head)
        if prix is None:
            prix = _parse_price(prix_el.get_text(" ", strip=True))

    # Photos
    photos = []
    for img in card.select("img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "notaires_fruchon_36",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",  # listing ne donne que le département, pas le CP complet
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Office Notarial FRUCHON & ASSOCIÉS",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    if not text:
        return None
    cleaned = text.split(",")[0]  # ignore d'éventuels centimes
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    if v is not None and v < 1000:  # garde-fou contre un "%" ou un nombre parasite
        return None
    return v


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
    print(f"\nTotal Fruchon & Associés (36): {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

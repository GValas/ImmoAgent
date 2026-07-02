"""scrapers/notaires_norial_45.py — NORIAL Notaires (office notarial, Orléans + Paris)

Méthode : scrape_simple (httpx) — SSR HTML (template Genapi/Noty)
URL liste : /annonces-immo/ventes.htm?page=N
            → liste GLOBALE de l'office (pas de param département serveur fiable :
              le filtre ville/dept est en formulaire POST/JS). On scrape toutes les
              pages puis on POST-FILTRE STRICTEMENT sur code_postal[:2] ∈ cibles.

Couverture : office notarial NORIAL (Orléans + Paris). Stock majoritairement dans
             le Loiret (45 — dept cible), un peu de Loir-et-Cher (41 — cible), et
             quelques biens parisiens (75/92 — HORS cible, écartés par post-filtre).

Cartes : li.item.annonce_type_vente
  - URL/titre : h4 > a[href]  → /annonces-immo/ventes/annonces/vente-{type}-t{N}-{NN}m2-{ville}-{cp}-{id}.htm
  - Prix      : .annoncePrix_valeur .montant .entier  → "128 960"
  - Ville     : .annonceAdresseVille
  - CP        : .annonceAdresseCp   → "45000"
  - Desc      : .annonceDesc
  - Réf       : .annonceRef  → "Réf. : 45007-2641"
  - Photo     : .annonceImage img[src]
  Le slug d'URL encode type / pièces (tN) / surface (NNm2) / ville / CP / id.

Type de bien : déduit du slug ; on ne garde que maisons / propriétés (appartements,
               terrains, locaux exclus).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://norial.notaires.fr"
LIST_PATH = "/annonces-immo/ventes.htm"
MAX_PAGES = 15
PHOTOS_PER_CARD = 6


# Départements cibles (post-filtre strict sur code_postal[:2])
TARGET_DEPTS = {"72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"}

# Types de bien (depuis le slug) à conserver / exclure
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|corps-de-ferme|maison-de-village|hotel",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|cave|cellier|box",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    # Restreint aux départements cibles connus du projet (sécurité)
    departements = departements & TARGET_DEPTS if departements else TARGET_DEPTS
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}{LIST_PATH}?page={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Norial] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                "li.item.annonce_type_vente"
            )
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

                cp = bien["code_postal"]
                # POST-FILTRE DÉPARTEMENT STRICT — 0 fuite hors-zone
                if not cp or cp[:2] not in departements:
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

                bien["departement"] = cp[:2]
                seen_ids.add(aid)
                results.append(bien)
                new_on_page += 1

            await asyncio.sleep(0.5)

    print(f"[Norial] Total {len(results)} annonces (zone cible)")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("h4 a[href]") or card.select_one(".annonceImage a[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

    slug = href.rsplit("/", 1)[-1]  # vente-{type}-t{N}-{NN}m2-{ville}-{cp}-{id}.htm
    slug_no_ext = re.sub(r"\.html?$", "", slug)

    # Type de bien depuis le slug
    type_seg = slug_no_ext
    if not _KEEP_TYPE.search(type_seg):
        # type non maison/propriété → exclu
        return None
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = _type_from_slug(slug_no_ext)

    # Localisation
    ville_el = card.select_one(".annonceAdresseVille")
    ville = ville_el.get_text(" ", strip=True) if ville_el else ""
    cp_el = card.select_one(".annonceAdresseCp")
    code_postal = cp_el.get_text(strip=True) if cp_el else ""
    m_cp = re.search(r"\d{5}", code_postal)
    code_postal = m_cp.group(0) if m_cp else ""
    if not code_postal:
        # secours : CP dans le slug
        m = re.search(r"-(\d{5})-\d+$", slug_no_ext)
        if m:
            code_postal = m.group(1)

    # Titre
    title_el = card.select_one("h4 a") or card.select_one("h4")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Description
    desc_el = card.select_one(".annonceDesc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Prix
    prix = None
    entier_el = card.select_one(".annoncePrix_valeur .montant .entier")
    if entier_el:
        prix = _parse_price(entier_el.get_text(" ", strip=True))
    if prix is None:
        prix_el = card.select_one(".annoncePrix_valeur")
        if prix_el:
            prix = _parse_price(prix_el.get_text(" ", strip=True))

    # Référence (id_annonce)
    ref_el = card.select_one(".annonceRef")
    ref = ""
    if ref_el:
        m = re.search(r"R[ée]f\.?\s*:?\s*([\w\-]+)", ref_el.get_text(" ", strip=True))
        ref = m.group(1) if m else ref_el.get_text(strip=True)
    # id numérique du slug en secours
    id_num = ""
    m_id = re.search(r"-(\d+)$", slug_no_ext)
    if m_id:
        id_num = m_id.group(1)
    id_annonce = ref or id_num or url

    # Surface / pièces depuis le slug
    surface = None
    m_surf = re.search(r"-(\d+)m2-", slug_no_ext)
    if m_surf:
        try:
            f = float(m_surf.group(1))
            if 8 <= f <= 5000:
                surface = f
        except ValueError:
            pass
    pieces = None
    m_t = re.search(r"-t(\d+)-", slug_no_ext)
    if m_t:
        pieces = int(m_t.group(1))

    # Photos
    photos = []
    for img in card.select(".annonceImage img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif not src.startswith("http"):
                src = f"{BASE_URL}/{src.lstrip('/')}"
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "notaires_norial_45",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "NORIAL Notaires",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _type_from_slug(slug: str) -> str:
    """'vente-maison-t4-80m2-fleury-...' → 'maison'."""
    m = re.search(
        r"vente-([a-zàâçéèêëîïôûùüÿñæœ]+(?:-de-[a-z]+)?)", slug, re.IGNORECASE
    )
    if m:
        word = m.group(1)
        return word.replace("-", " ").strip()
    return "maison"


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r",\d{2}$", "", cleaned)  # retire les centimes ",00"
    cleaned = re.sub(r"[^\d]", "", cleaned)
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
    print(f"\nTotal Norial: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

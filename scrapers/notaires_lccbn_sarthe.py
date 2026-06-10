"""scrapers/notaires_lccbn_sarthe.py — LCC.BN Notaires (Sarthe)

Office notarial LCC.BN (Le Mans / Noyen-sur-Sarthe / Loué / Conlie /
Coulans-sur-Gée). Cœur d'activité : Sarthe (72), avec quelques biens en
Maine-et-Loire (49) et Mayenne (53).

Méthode : scrape_simple (httpx) — SSR HTML (rendu serveur complet, pas de JS).
URL pattern : /annonces-immobilieres.htm?page={N}
              → PAS de filtre département côté serveur (office mono-secteur).
              On récupère tout, puis post-filtre STRICT sur code_postal[:2].

Pagination : ?page=1..N (6 cartes/page). Quand la page dépasse le dernier lot,
le site re-sert la dernière page → on déduplique par référence et on stoppe dès
qu'une page n'apporte aucune annonce nouvelle.

Cartes : li.item.annonce_type_vente
  - URL   : h4 a[href]  → annonces-immobilieres/annonces/{slug}.htm
            slug = vente-{type}-t{N}-{surface}m2-{ville}-{cp}-{id}.htm
  - Titre : h4 a
  - Prix  : .annoncePrix .montant .entier  →  "230 000"  (+ .cents ",00")
  - Ville : .annonceAdresseVille
  - CP    : .annonceAdresseCp  →  "72300"
  - Desc  : .annonceDesc
  - Réf   : .annonceRef  →  "Réf. : 72120-1041"
  - Photo : .annonceImage img[src]

Type de bien / surface / pièces : déduits du slug d'URL
  (vente-maison-t6-150m2-... → maison, 6 pièces, 150 m²).
  On ne garde que maisons / propriétés (appartement & terrain exclus).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://lccbn.notaires.fr"
LISTING = f"{BASE_URL}/annonces-immobilieres.htm"
MAX_PAGES = 20
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

# Types de bien à conserver (depuis le slug d'URL) : maisons / propriétés...
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|"
    r"maison-de-village|fermette|grange",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    # On ne traite que les départements à la fois demandés ET dans la zone cible
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
            url = f"{LISTING}?page={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[LCCBN] Erreur page {page}: {e}")
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

                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                new_on_page += 1

                # Post-filtre dept STRICT (pas de filtre serveur)
                cp = bien["code_postal"]
                if not cp or cp[:2] not in allowed:
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

            # Page sans aucune annonce nouvelle (re-service de la dernière page)
            if new_on_page == 0:
                break

            await asyncio.sleep(0.5)

    print(f"[LCCBN] {len(results)} annonces retenues (zone cible)")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("h4 a") or card.select_one("a.annonceLireSuite")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

    # Slug : vente-{type}-t{N}-{surface}m2-{ville}-{cp}-{id}.htm
    slug = href.rsplit("/", 1)[-1].replace(".htm", "")

    # id annonce (dernier segment numérique du slug) + réf affichée
    m_id = re.search(r"-(\d+)$", slug)
    slug_id = m_id.group(1) if m_id else ""
    ref_el = card.select_one(".annonceRef")
    ref = ""
    if ref_el:
        ref = re.sub(r"^R[ée]f\.?\s*:?\s*", "", ref_el.get_text(strip=True))
    id_annonce = slug_id or ref or url

    # Type de bien depuis le slug
    type_seg = slug.split("-t")[0] if "-t" in slug else slug
    type_seg = re.sub(r"^vente-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg.replace("-", " ").strip() or "maison"

    # Pièces : segment -tN-
    pieces = None
    m_p = re.search(r"-t(\d+)-", slug)
    if m_p:
        pieces = int(m_p.group(1))

    # Surface : segment {N}m2 (peut être groupé par milliers : "2-150m2" = 2150)
    surface = None
    m_s = re.search(r"-((?:\d+-)?\d+)m2-", slug)
    if m_s:
        try:
            surface = float(m_s.group(1).replace("-", ""))
        except ValueError:
            surface = None

    # Ville / CP
    ville_el = card.select_one(".annonceAdresseVille")
    ville = ville_el.get_text(" ", strip=True) if ville_el else ""
    cp_el = card.select_one(".annonceAdresseCp")
    code_postal = ""
    if cp_el:
        m_cp = re.search(r"\d{5}", cp_el.get_text(strip=True))
        if m_cp:
            code_postal = m_cp.group(0)
    if not code_postal:
        # secours : CP dans le slug
        m_cp = re.search(r"-(\d{5})-\d+$", slug)
        if m_cp:
            code_postal = m_cp.group(1)

    # Titre
    title_el = card.select_one("h4 a") or card.select_one("h4")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Description
    desc_el = card.select_one(".annonceDesc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Prix : .annoncePrix .entier (+ .cents)
    prix = None
    montant = card.select_one(".annoncePrix .montant")
    if montant:
        ent = montant.select_one(".entier")
        if ent:
            prix = _parse_price(ent.get_text(" ", strip=True))
    if prix is None:
        prix_el = card.select_one(".annoncePrix")
        if prix_el:
            prix = _parse_price(prix_el.get_text(" ", strip=True))

    # Photos
    photos = []
    for img in card.select(".annonceImage img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif not src.startswith("http"):
                src = f"{BASE_URL}/{src.lstrip('/')}"
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    dept = code_postal[:2] if code_postal else ""

    return {
        "source": "notaires_lccbn_sarthe",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "LCC.BN Notaires",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = cleaned.split(",")[0]  # ignore les centimes
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
    print(f"\nTotal LCC.BN Notaires: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

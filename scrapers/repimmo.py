"""scrapers/repimmo.py — Repimmo (portail / annuaire d'annonces immobilières)

Méthode : scrape_simple (httpx) — SSR HTML pur (microdata schema.org).
URL pattern : http://www.repimmo.com/annonces-immobilieres-gratuites-{slug}/vente-maison-{slug}-{NN}/?page=P
              → filtre département + type "maison" CÔTÉ SERVEUR
              (vérifié : aucune fuite hors-dept sur les 11 départements cibles).
              NB : le site sert le HTTPS avec un certificat expiré/auto-signé ;
              on requête donc en HTTP (le contenu est identique et public).

Cartes : div.annonce_resume  (article > div.bloc_standard.annonce_resume)
  - URL    : h3 > a[href]  → /petite_annonces_immobiliere/{id}/{type}-a_vendre-{ville}-{NN}.php
  - Titre  : h3 > a (itemprop=name)  →  "Maison 5 pièces 143 m²"  (type, pièces, surface)
  - Prix   : h3 > span               →  "507 000 €"
  - Loc    : .annonce_resume_prix    →  "Vente maison 143 m2 sur Crouy-sur-cosson ( 41220 - Loir et cher )"
             addressLocality = ville ; "( CODEPOSTAL - département )"
  - Texte  : p.annonce_description_txt (itemprop=description)
  - Photos : .annonce_bloc_photo img.imageImmo[src]  (+ compteur .imageImmoEcorne)
  - Agence : .annonce_resume_footer_adresse a

Type de bien : on cible déjà l'URL "vente-maison-…" côté serveur ; on re-filtre
               malgré tout sur le titre/URL pour écarter terrains/appartements.

Pagination : ?page=N (la 1re page est sans paramètre). On s'arrête dès qu'une
             page ne ramène aucune carte nouvelle.

Couverture : portail national agrégeant des agences ; volume réel et SSR sur les
             11 départements cibles (≥10 maisons/page, plusieurs dizaines de pages
             par département sur 41/45/37…).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "http://www.repimmo.com"
MAX_PAGES = 10
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Code département → slug URL repimmo (annonces-immobilieres-gratuites-{slug}/vente-maison-{slug}-{NN})
DEPT_SLUGS: dict[str, str] = {
    "72": "sarthe",
    "28": "eure-et-loir",
    "45": "loiret",
    "89": "yonne",
    "49": "maine-et-loire",
    "37": "indre-et-loire",
    "36": "indre",
    "18": "cher",
    "58": "nievre",
    "41": "loir-et-cher",
    "53": "mayenne",
}

# Type de bien à conserver (sécurité, l'URL serveur cible déjà "maison")
_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|mas|gite|g[iî]te|corps-de-ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"terrain|appartement|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"parking|cave|box|hangar",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30, verify=False
    ) as client:
        for dept in departements:
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, slug, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[Repimmo] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Repimmo] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    slug: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    base = f"{BASE_URL}/annonces-immobilieres-gratuites-{slug}/vente-maison-{slug}-{dept}/"

    for page in range(1, MAX_PAGES + 1):
        url = base if page == 1 else f"{base}?page={page}"
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select("div.annonce_resume")
        if not cards:
            break

        new_ids_on_page = 0
        for card in cards:
            try:
                bien = _parse_card(card, dept)
            except Exception:
                continue
            if not bien:
                continue

            # Post-filtre dept STRICT (le filtre serveur est OK, on re-vérifie)
            if bien["code_postal"] and bien["code_postal"][:2] != dept:
                continue

            aid = bien["id_annonce"]
            if aid in seen_ids:
                continue
            seen_ids.add(aid)
            new_ids_on_page += 1

            # Filtres de critères (n'arrêtent PAS la pagination)
            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            biens.append(bien)

        # Fin réelle de pagination : aucune NOUVELLE annonce (page répétée/vide)
        if new_ids_on_page == 0:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    h3 = card.find("h3")
    link = h3.find("a") if h3 else None
    href = link.get("href", "") if link else ""
    if not href or "/petite_annonces_immobiliere/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Titre  →  "Maison 5 pièces 143 m²"
    titre = link.get_text(" ", strip=True)

    # Filtre type (sécurité) sur titre + URL
    haystack = f"{titre} {href}"
    if _EXCLUDE_TYPE.search(haystack) and not _KEEP_TYPE.search(titre):
        return None
    if not _KEEP_TYPE.search(haystack):
        return None
    type_bien = "maison"
    m_type = re.match(r"\s*([A-Za-zÀ-ÿ'\- ]+?)\s+\d+\s*pi[eè]ce", titre)
    if m_type:
        type_bien = m_type.group(1).strip().lower()

    # id_annonce depuis l'URL  → /petite_annonces_immobiliere/{id}/...
    m_id = re.search(r"/petite_annonces_immobiliere/(\d+)/", href)
    id_annonce = m_id.group(1) if m_id else url

    # Localisation : ".annonce_resume_prix" → "… sur Ville ( CP - dépt )"
    addr_el = card.select_one(".annonce_resume_prix")
    addr_text = addr_el.get_text(" ", strip=True) if addr_el else ""
    ville, code_postal = _parse_loc(addr_el, addr_text)

    # Prix : h3 > span
    price_el = h3.find("span") if h3 else None
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Pièces & surface : depuis le titre, secours depuis l'adresse
    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", titre)
    surface = _parse_surface(titre) or _parse_surface(addr_text)

    # Description
    desc_el = card.select_one(".annonce_description_txt")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Photos
    photos = []
    for img in card.select(".annonce_bloc_photo img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "http:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    # Agence
    ag_el = card.select_one(".annonce_resume_footer_adresse a")
    agence = ag_el.get_text(" ", strip=True) if ag_el else "Repimmo"

    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    return {
        "source": "repimmo",
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
        "agence": agence,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(addr_el, addr_text: str) -> tuple[str, str]:
    """addressLocality + '( CODEPOSTAL - département )' → (ville, cp)."""
    ville = ""
    if addr_el is not None:
        loc_el = addr_el.find(attrs={"itemprop": "addressLocality"})
        if loc_el:
            ville = loc_el.get_text(" ", strip=True)
    if not ville:
        m_v = re.search(r"\bsur\s+(.+?)\s*\(", addr_text)
        if m_v:
            ville = m_v.group(1).strip()
    cp = ""
    m_cp = re.search(r"\(\s*(\d{5})\s*-", addr_text)
    if m_cp:
        cp = m_cp.group(1)
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_surface(text: str) -> float | None:
    """'Maison 5 pièces 143 m²' / '143 m2' → 143.0 (surface habitable)."""
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0]*)\s*m(?:²|2)\b", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 3000:
                return f
        except ValueError:
            pass
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
    print(f"\nTotal Repimmo: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

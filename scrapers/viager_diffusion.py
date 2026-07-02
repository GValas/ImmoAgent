"""scrapers/viager_diffusion.py — Viager Diffusion (portail national de viager)

Méthode : scrape_simple (httpx) — SSR HTML
Portail spécialisé en viager / vente à terme / nue-propriété / vente au comptant
(viager sans rente). Couverture nationale, inventaire concentré mais réel dans
plusieurs départements cibles (Yonne 89 inclus).

URL pattern (filtre département CÔTÉ SERVEUR) :
    /extra/listing.php?vl[]=d{NN}&_f_v=1&_l_i_p={page}
  - vl[]      : identifiant de localisation. Pour un département : "d{NN}"
                (ex. d89). Ces ids proviennent de l'autocomplétion
                /extra/xhr/localisation.php?q=... (q="89" → {"id":"d89", ...}).
                → filtre serveur fiable (vérifié : aucune fuite hors-dept).
  - _f_v=1    : marque le formulaire comme soumis.
  - _l_i_p    : numéro de page (pagination).

Cartes résultat : article.cell.small-12.medium-6.large-4 (la 1ʳᵉ <article> de la
  page est le formulaire de recherche, sans cette classe → ignorée).
  - URL/id : a[href*="detail.php"]  → /extra/detail.php?id={id}
  - Type vente : h3 (Viager occupé / Nue-propriété… → renseigne type_bien)
  - Nature + surface : div avec <i class="mdi-home-variant"> → "Immeuble mixte 217 m²"
  - Localisation : div avec <i class="mdi-map-marker"> → "89000 AUXERRE"
  - Bouquet / rente : .card-divider → "Bouquet : 28 700 € / Rente mensuelle : 328 €"
  - Photos : .orbit-figure img[src] (galerie)
  - Âge crédirentier : .label (ex "65 ans"), occupation estimée : label orange.

Spécificités viager :
  - `prix` = bouquet (comptant) — PAS le prix de vente classique. Les bornes
    prix_min/prix_max du projet (300k–600k) ne sont donc PAS appliquées ici
    (un bouquet viager est structurellement bien inférieur). surface_min est
    appliqué quand la surface est connue.
  - On laisse passer toutes les natures de bien (le filtre structurel type/pièces
    du hunter opère en aval) ; type_bien est dérivé de la nature affichée.

Filtre dept : serveur (vl[]=dNN) + post-filtre strict code_postal[:2] == dept.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.viager-diffusion.com"
LISTING_URL = BASE_URL + "/extra/listing.php"
MAX_PAGES = 8
PHOTOS_PER_CARD = 10


# Sélecteur des cartes résultat (la carte-formulaire n'a pas cette classe)
CARD_SELECTOR = "article.cell.small-12.medium-6.large-4"


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            try:
                biens = await _scrape_dept(client, dept, surface_min)
                results.extend(biens)
                print(f"[ViagerDiffusion] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[ViagerDiffusion] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient, dept: str, surface_min: int
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        params = [("vl[]", f"d{dept}"), ("_f_v", "1"), ("_l_i_p", str(page))]
        r = await client.get(LISTING_URL, params=params)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select(CARD_SELECTOR)
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            try:
                bien = _parse_card(card, dept)
            except Exception:
                continue
            if not bien:
                continue

            # Filtre dept STRICT : on n'accepte que le département cible.
            if bien["code_postal"] and bien["code_postal"][:2] != dept:
                continue

            aid = bien["id_annonce"]
            if aid in seen_ids:
                continue

            s = bien.get("surface") or 0
            if surface_min and s and s < surface_min:
                continue

            seen_ids.add(aid)
            biens.append(bien)
            new_on_page += 1

        if new_on_page == 0:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one('a[href*="detail.php"]')
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href
    m_id = re.search(r"id=(\d+)", href)
    id_annonce = m_id.group(1) if m_id else url

    # Type de vente (h3) → sert de type_bien + titre
    h3 = card.select_one(".card-section h3")
    type_vente = h3.get_text(" ", strip=True) if h3 else ""

    # Nature + surface : div contenant l'icône home-variant
    nature = ""
    surface = None
    home_div = _div_with_icon(card, "mdi-home-variant")
    if home_div:
        nature_txt = home_div.get_text(" ", strip=True)
        # ex: "Immeuble mixte 217 m²"
        m_s = re.search(r"([\d\s\xa0]+)\s*m²", nature_txt)
        if m_s:
            val = re.sub(r"[\s\xa0]", "", m_s.group(1))
            try:
                f = float(val)
                if 5 <= f <= 5000:
                    surface = f
            except ValueError:
                pass
        nature = re.sub(r"[\d\s\xa0]+\s*m².*$", "", nature_txt).strip()

    # Localisation : div contenant l'icône map-marker → "89000 AUXERRE"
    code_postal = ""
    ville = ""
    loc_div = _div_with_icon(card, "mdi-map-marker")
    if loc_div:
        loc_txt = loc_div.get_text(" ", strip=True)
        m_cp = re.search(r"\b(\d{5})\b", loc_txt)
        if m_cp:
            code_postal = m_cp.group(1)
        ville = re.sub(r"\b\d{5}\b", "", loc_txt).strip()

    # Bouquet / rente (.card-divider)
    prix = None
    rente = None
    divider = card.select_one(".card-divider")
    if divider:
        dtxt = divider.get_text(" ", strip=True)
        m_b = re.search(r"Bouquet\s*:?\s*([\d\s\xa0]+)\s*€", dtxt, re.IGNORECASE)
        if m_b:
            prix = _to_float(m_b.group(1))
        m_r = re.search(
            r"Rente\s+mensuelle\s*:?\s*([\d\s\xa0]+)\s*€", dtxt, re.IGNORECASE
        )
        if m_r:
            rente = _to_float(m_r.group(1))

    # Type de bien : déduit de la nature (maison/appartement/immeuble/château…)
    type_bien = _normalize_type(nature) or "viager"

    # Titre
    titre_parts = [nature or type_vente, ville]
    titre = " — ".join([p for p in titre_parts if p]).strip() or "Viager"

    # Description courte synthétique
    desc_bits = []
    if type_vente:
        desc_bits.append(type_vente)
    if nature:
        desc_bits.append(nature)
    if surface:
        desc_bits.append(f"{int(surface)} m²")
    if prix:
        desc_bits.append(f"bouquet {int(prix)} €")
    if rente:
        desc_bits.append(f"rente {int(rente)} €/mois")
    description = " - ".join(desc_bits)

    # Âge crédirentier (label) — info viager utile
    age = None
    for lab in card.select(".label"):
        mt = re.search(r"(\d+)\s*ans", lab.get_text(" ", strip=True))
        if mt:
            age = int(mt.group(1))
            break
    if age:
        description += f" - crédirentier {age} ans"

    # Photos (galerie orbit)
    photos = []
    for img in card.select(".orbit-figure img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "viager_diffusion",
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
        "pieces": None,
        "chambres": None,
        "prix": prix,  # = bouquet/comptant (viager), pas un prix de vente classique
        "photos": photos,
        "dpe": None,
        "agence": "Viager Diffusion",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _div_with_icon(card, icon_class: str):
    """Retourne le <div> de la carte-section contenant un <i> de classe icon_class."""
    for i in card.select(".card-section i.mdi"):
        classes = i.get("class", [])
        if icon_class in classes:
            return i.parent
    return None


def _to_float(text: str) -> float | None:
    cleaned = re.sub(r"[\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _normalize_type(nature: str) -> str:
    n = (nature or "").lower()
    if "maison" in n or "villa" in n:
        return "maison"
    if "château" in n or "chateau" in n:
        return "propriete"
    if "hôtel particulier" in n or "hotel particulier" in n:
        return "maison"
    if "appartement" in n:
        return "appartement"
    if "immeuble" in n:
        return "immeuble"
    if "loft" in n or "atelier" in n:
        return "loft"
    if "terrain" in n:
        return "terrain"
    return nature.strip().lower()


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
    print(f"\nTotal Viager Diffusion: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:15]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — bouquet {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

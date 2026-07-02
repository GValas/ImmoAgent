"""scrapers/notaires_odin_89.py — Étude notariale Jean-Marie ODIN (Vermenton, 89)

Méthode : scrape_simple (httpx) — SSR HTML (template Prisme/Notariat Services).
URL : https://www.odin.notaires.fr/annonces-immobilieres/recherche.html
      → page unique de résultats (portefeuille de l'étude, ~14 biens à la vente).
      Pas de filtre département côté serveur : l'étude est mono-secteur (Yonne 89,
      autour de Vermenton : Arcy-sur-Cure, Bazarnes, Tonnerre, Jussy, Mouffy…).
      On post-filtre tout de même STRICTEMENT sur code_postal[:2] ∈ départements
      cibles → 0 fuite garantie.

Cartes : div.ns-property-card
  - URL    : a[href*="/annonce/"]
             /annonces-immobilieres/annonce/{id}/{type}-a-vendre-{ville}-{cp}-{N}m2-{P}pieces.html
             → l'URL encode aussi type, ville, CP, surface, nb pièces (secours fiable).
  - Type   : .c__type           → "Achat - Maison" / "Achat - Divers"
  - Loc    : .c__location       → "Arcy-sur-Cure - 89270" (ou "Vermenton (Sacy) - 89270")
  - Prix   : .c__price b        → "64 000 €"
  - Réf    : .prop__reference   → "Réf: ARCY"
  - Excerpt: .c__excerpt        → description courte
  - Quick  : .qi__bubble        → surface (em.fa-home), terrain (em.fa-leaf),
                                   pièces / chambres (sans icône, data-tipso)
  - Photos : .slider-properties img.img-responsive[src]

Particularité : le HTML est servi en double-encodage latin-1/utf-8 (mojibake) ;
                _fix_mojibake() le corrige.

Type de bien : on ne garde que maisons / propriétés (exclut "Divers", terrains…).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.odin.notaires.fr"
SEARCH_URL = f"{BASE_URL}/annonces-immobilieres/recherche.html"
PHOTOS_PER_CARD = 10


# Types de bien à conserver (segment d'URL / libellé) : maisons / propriétés…
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|pavillon",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|divers|viager",
    re.IGNORECASE,
)


def _fix_mojibake(text: str) -> str:
    """Corrige le double-encodage latin-1↔utf-8 ('Ã ' → 'à')."""
    if not text:
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        try:
            r = await client.get(SEARCH_URL)
        except Exception as e:
            print(f"[OdinNotaires89] Erreur requête: {e}")
            return results
        if r.status_code != 200:
            print(f"[OdinNotaires89] HTTP {r.status_code}")
            return results

        # Utiliser r.content (bytes) puis corriger le mojibake au parsing
        cards = BeautifulSoup(r.content, "html.parser").select(".ns-property-card")
        if not cards:
            print("[OdinNotaires89] Aucune carte trouvée")
            return results

        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue

            # Post-filtre département STRICT (0 fuite)
            cp = bien["code_postal"]
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

            seen_ids.add(aid)
            results.append(bien)

    print(f"[OdinNotaires89] {len(results)} annonces (zone cible)")
    await asyncio.sleep(0.5)
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one('a[href*="/annonce/"]')
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce : segment {id} de l'URL (/annonce/{id}/...)
    id_annonce = ""
    m_id = re.search(r"/annonce/([^/]+)/", href)
    if m_id:
        id_annonce = m_id.group(1)

    # Slug descriptif (dernier segment) : {type}-a-vendre-{ville}-{cp}-{N}m2-{P}pieces
    slug = href.rstrip("/").split("/")[-1]
    slug = re.sub(r"\.html?$", "", slug, flags=re.IGNORECASE)

    # Type de bien (libellé carte, secours sur le slug)
    type_el = card.select_one(".c__type")
    type_label = ""
    if type_el and type_el.contents:
        type_label = _fix_mojibake(str(type_el.contents[0]).strip())
    type_for_filter = f"{type_label} {slug}"
    if _EXCLUDE_TYPE.search(type_for_filter) and not _KEEP_TYPE.search(type_for_filter):
        return None
    if not _KEEP_TYPE.search(type_for_filter):
        return None
    type_bien = "maison"
    m_t = re.match(r"^([a-zàâäéèêëîïôöùûüç-]+)-a-vendre", slug, re.IGNORECASE)
    if m_t:
        type_bien = m_t.group(1).replace("-", " ")
    elif "-" in type_label:
        type_bien = type_label.split("-")[-1].strip().lower() or "maison"

    # Localisation : "Ville - 89270"
    loc_el = card.select_one(".c__location")
    loc = _fix_mojibake(loc_el.get_text(" ", strip=True)) if loc_el else ""
    ville, code_postal = _parse_loc(loc)
    if not code_postal:
        # secours : CP dans le slug d'URL
        m_cp = re.search(r"-(\d{5})-", slug)
        if m_cp:
            code_postal = m_cp.group(1)
        if not ville:
            m_v = re.search(r"a-vendre-(.+?)-\d{5}-", slug)
            if m_v:
                ville = m_v.group(1).replace("-", " ").title()

    # Référence
    ref_el = card.select_one(".prop__reference")
    ref = ""
    if ref_el:
        ref = re.sub(r"^R[ée]f\s*:\s*", "", _fix_mojibake(ref_el.get_text(strip=True)),
                     flags=re.IGNORECASE).strip()

    # Prix
    price_el = card.select_one(".c__price b")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Description (excerpt)
    ex_el = card.select_one(".c__excerpt")
    description = _fix_mojibake(ex_el.get_text(" ", strip=True)) if ex_el else ""

    # Quick infos : surface (fa-home), terrain (fa-leaf), pièces / chambres
    surface = surface_terrain = pieces = chambres = None
    for li in card.select(".qi__bubble"):
        icon = li.select_one("em.fa")
        icon_cls = " ".join(icon.get("class", [])) if icon else ""
        val_el = li.select_one("strong, b")
        if not val_el:
            continue
        num = _to_num(val_el.get_text(strip=True))
        if num is None:
            continue
        tip_el = li.select_one(".tipso_top")
        tip = (tip_el.get("data-tipso") or "") if tip_el else ""
        small = li.select_one("small")
        small_txt = small.get_text(" ", strip=True).lower() if small else ""

        if "fa-home" in icon_cls:
            surface = float(num)
        elif "fa-leaf" in icon_cls:
            surface_terrain = float(num)
        elif "pi" in tip.lower() and "p" in small_txt and "chb" not in small_txt:
            pieces = int(num)
        elif "chb" in small_txt:
            chambres = int(num)

    # Secours via le slug d'URL : {N}m2-{P}pieces
    if surface is None:
        m_s = re.search(r"-(\d+)m2", slug)
        if m_s:
            surface = float(m_s.group(1))
    if pieces is None:
        m_p = re.search(r"-(\d+)piece", slug)
        if m_p:
            pieces = int(m_p.group(1))

    # Titre
    titre = type_bien.title()
    if ville:
        titre += f" à {ville}"
    if surface:
        titre += f" — {int(surface)} m²"

    # Photos
    photos = []
    for img in card.select(".slider-properties img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    dept = code_postal[:2] if code_postal else ""

    return {
        "source": "notaires_odin_89",
        "url": url,
        "id_annonce": ref or id_annonce or url,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Étude Jean-Marie ODIN (notaire, Vermenton 89)",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Arcy-sur-Cure - 89270' → ('Arcy-sur-Cure', '89270')"""
    cp = ""
    m_cp = re.search(r"(\d{5})", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*-?\s*\d{5}\s*$", "", text).strip(" -").strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text.split("+")[0])
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _to_num(text: str) -> float | None:
    cleaned = re.sub(r"[\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d.]", "", cleaned)
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
    print(f"\nTotal Odin Notaires 89: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b.get('pieces') or '?'}p/{b.get('chambres') or '?'}chb"
            f" — {b['ville']}"
        )

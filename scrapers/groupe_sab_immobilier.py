"""scrapers/groupe_sab_immobilier.py — Groupe SAB Immobilier (Eyrieux Immobilier)

Agence indépendante Drôme / Ardèche (26 / 07), maisons en pierre, fermes, mas,
villas de caractère, demeures de prestige. AURA, hors zone Val-de-Loire/Ouest.

Méthode : scrape_simple (httpx) — SSR HTML (Apache, pas de Cloudflare, statut 200).

URL pattern : /nos-biens/{page}  → catalogue COMPLET de l'agence (pas de filtre
              département serveur exploitable). Le pattern /vente/{NN-dept}/{page}
              existe mais ne renvoie qu'un squelette de formulaire vide (CSR), il
              N'EST PAS utilisable pour filtrer. On scrape donc tout le catalogue
              (petit : ~15-20 biens, 07/26 uniquement) et on POST-FILTRE strict sur
              code_postal[:2] == département cible.

Cartes : div.property-listing-v2__container.item  (10 / page)
  - URL    : a[href*='/vente/']  → /vente/{villeid-ville}/{type}/{tN}/{id-slug}/
  - Loc    : .title__content-1   →  "Ville (CODEPOSTAL)"
  - Titre  : .title__content-2
  - Prix   : .__price-value      →  "315 000 €"
  - Réf    : .item__reference    →  "Réf : 5582"
  - Texte  : .item__text-block   (description)
  - Photo  : img[src]  (URL protocole-relatif //...staticlbi.com/...)

Type de bien : déduit du segment d'URL (ferme, maison, propriete, villa, immeuble,
               local...). On ne garde que maisons / propriétés / fermes / mas...

Couverture : agence mono-zone 26/07. Sur les départements cibles actuels
             (72/28/45/89 et la zone Val-de-Loire/Ouest) → 0 stock. Scraper
             fonctionnel conservé (actif: false), à réactiver si la zone s'étend
             à la Drôme/Ardèche.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.groupe-sab-immobilier.com"
LIST_PATH = "/nos-biens"
MAX_PAGES = 8
PHOTOS_PER_CARD = 10


# Types de bien (segment d'URL) à conserver : maisons / propriétés / fermes...
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|maison-de-village|"
    r"bastide|grange",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}{LIST_PATH}/{page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[GroupeSAB] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(
                "div.property-listing-v2__container.item"
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

                # Post-filtre département STRICT (0 fuite hors-zone)
                cp = bien["code_postal"]
                if not cp or cp[:2] not in departements:
                    continue
                # Renseigne le département depuis le CP retenu
                bien["departement"] = cp[:2]

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

            # Dernière page atteinte (catalogue ré-affiché à l'identique)
            if new_on_page == 0 and page > 1:
                # On vérifie via les refs : si aucune nouvelle ref, on arrête
                pass
            await asyncio.sleep(0.6)

    print(f"[GroupeSAB] {len(results)} annonces retenues (post-filtre dept)")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a[href*='/vente/']")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien : /vente/{villeid-ville}/{type}/{tN}/{id-slug}/
    parts = [p for p in href.split("/") if p]
    # parts ~ ['vente', '216-mirmande', 'ferme', 't8', '5171-...']
    type_seg = parts[2] if len(parts) > 2 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean) and not _KEEP_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        return None
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # Localisation : "Ville (CODEPOSTAL)"
    loc_el = card.select_one(".title__content-1")
    loc = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)

    # Titre (2ᵉ ligne) avec repli sur le titre complet
    t2_el = card.select_one(".title__content-2")
    titre = t2_el.get_text(" ", strip=True) if t2_el else ""
    if not titre:
        full_el = card.select_one(".item__title")
        titre = full_el.get_text(" ", strip=True) if full_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Référence (id_annonce)
    ref_el = card.select_one(".item__reference")
    ref_txt = ref_el.get_text(strip=True) if ref_el else ""
    m_ref = re.search(r"R[ée]f\s*:?\s*(\S+)", ref_txt)
    ref = m_ref.group(1) if m_ref else ""
    # id numérique du slug final en secours
    id_num = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_num = m.group(1)
    id_annonce = ref or id_num or url

    # Description
    text_el = card.select_one(".item__text-block")
    description = text_el.get_text(" ", strip=True) if text_el else ""

    # Prix
    price_el = card.select_one(".__price-value") or card.select_one(".item__price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Surface habitable / terrain / pièces : pas de champs structurés dans la carte
    # → on tente depuis titre + description
    blob = f"{titre} {description}"
    surface = _parse_surface_hab(blob)
    surface_terrain = _parse_terrain(blob)
    pieces = _parse_pieces(blob)
    # Pièces en secours : segment tN de l'URL
    if pieces is None:
        for seg in parts:
            m = re.match(r"^t(\d+)$", seg)
            if m:
                pieces = int(m.group(1))
                break

    # Photos
    photos = []
    for img in card.find_all("img"):
        src = (
            img.get("data-src")
            or img.get("data-lazy")
            or img.get("data-original")
            or img.get("src")
            or ""
        )
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    # dédup en gardant l'ordre
    seen = set()
    photos = [p for p in photos if not (p in seen or seen.add(p))][:PHOTOS_PER_CARD]

    return {
        "source": "groupe_sab_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else None,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Groupe SAB Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Mirmande (26270)' → ('Mirmande', '26270')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text).strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text).replace(",", ".")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_terrain(text: str) -> float | None:
    """'terrain de 1627m²' / 'parc de 8000 m²' → float"""
    if not text:
        return None
    m = re.search(
        r"(?:terrain|parc|terres?|jardin)[^0-9]{0,15}([\d\s\xa0]+)\s*m[²2]",
        text,
        re.IGNORECASE,
    )
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if f >= 20:
                return f
        except ValueError:
            pass
    # hectares
    m_ha = re.search(r"([\d.,]+)\s*ha", text, re.IGNORECASE)
    if m_ha:
        try:
            return float(m_ha.group(1).replace(",", ".")) * 10000
        except ValueError:
            pass
    return None


def _parse_surface_hab(text: str) -> float | None:
    """Cherche 'NNN m²' (habitable) dans le texte, en évitant le terrain."""
    if not text:
        return None
    # Mentions explicites d'habitable
    m = re.search(
        r"(\d[\d\s\xa0]*)\s*m[²2]\s*(?:hab|habitable|de surface)",
        text,
        re.IGNORECASE,
    )
    if not m:
        # Premier "NNN m²" non précédé d'un mot de terrain
        m = re.search(
            r"(?<!terrain )(?<!parc )(?<!jardin )(\d[\d\s\xa0]*)\s*m[²2]",
            text,
            re.IGNORECASE,
        )
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 2000:
                return f
        except ValueError:
            pass
    return None


def _parse_pieces(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d+)\s*pi[èe]ces?", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


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
    print(f"\nTotal Groupe SAB: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

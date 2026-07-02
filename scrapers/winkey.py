"""scrapers/winkey.py — Winkey Immobilier (réseau de mandataires national, créé 2018)

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /vente/{NN-dept-slug}/{page}   (ex: /vente/72-sarthe/1)
              → filtre département CÔTÉ SERVEUR (vérifié : aucune fuite hors-dept,
                toutes les annonces d'une page dept ont bien le préfixe CP attendu).

Cartes : article.property-listing-v1__item.item
  - URL détail : .js-obfuscation[data-url]
                 → /vente/{NN-dept}/{cityid-ville}/{typeid-type}/{tN}/{id-slug}/
  - Titre/loc  : .item__title  →  "Ville (CODEPOSTAL) Reste du titre…"
  - Prix       : .item__price  →  "466 000 €"
  - Options    : .item__options  →  "7 Pièce(s) 4 Chambre(s) 1 Salle(s) de bain"
  - Référence  : .item__reference  →  "Réf : VMA2080006135"
  - Photos     : img.item__img[src]  (URL en //winkeys.staticlbi.com/…)

Type de bien : déduit du segment d'URL (1-maison, 2-appartement, 3-terrain…).
               On ne garde que maisons / propriétés / villas.

Surface habitable : absente des options → extraite du titre ("… 208m² …") quand
                    présente, sinon None.
Terrain / DPE : non exposés sur la carte de liste → None.

Couverture : réseau national à implantation inégale ; sur la zone cible l'inventaire
             est faible mais réel (72, 28, 49, 37, 53 ont des biens ; 45/89/36/18/58/41
             à 0 au dernier test).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.winkey.fr"
MAX_PAGES = 6
PHOTOS_PER_CARD = 10


# Code département → slug URL winkey.fr/vente/{NN-slug}/{page}
DEPT_SLUGS: dict[str, str] = {
    "72": "72-sarthe",
    "28": "28-eure-et-loir",
    "45": "45-loiret",
    "89": "89-yonne",
    "49": "49-maine-et-loire",
    "37": "37-indre-et-loire",
    "36": "36-indre",
    "18": "18-cher",
    "58": "58-nievre",
    "41": "41-loir-et-cher",
    "53": "53-mayenne",
}

# Types de bien (segment d'URL ou libellé) à conserver : maisons / propriétés…
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|corps-de-ferme|maison-de-village",
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

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
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
                print(f"[Winkey] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Winkey] Erreur dept {dept}: {e}")
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

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/vente/{slug}/{page}"
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select(
            "article.property-listing-v1__item"
        )
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

            # Sécurité : on n'accepte que le département cible (filtre serveur déjà OK)
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
            biens.append(bien)
            new_on_page += 1

        if new_on_page == 0:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    # URL détail (obfusquée mais lisible dans data-url)
    obf = card.select_one(".js-obfuscation")
    href = obf.get("data-url", "") if obf else ""
    if not href:
        # secours : un éventuel <a href>
        a = card.select_one("a[href*='/vente/']")
        href = a.get("href", "") if a else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL :
    # /vente/{NN-dept}/{cityid-ville}/{typeid-type}/{tN}/{id-slug}/
    parts = [p for p in href.split("/") if p]
    type_seg = parts[3] if len(parts) > 3 else ""
    type_seg_clean = re.sub(r"^\d+-", "", type_seg)
    if _EXCLUDE_TYPE.search(type_seg_clean) and not _KEEP_TYPE.search(type_seg_clean):
        return None
    if not _KEEP_TYPE.search(type_seg_clean):
        return None
    type_bien = type_seg_clean.replace("-", " ").strip() or "maison"

    # Référence (id_annonce)
    ref_el = card.select_one(".item__reference")
    ref_txt = ref_el.get_text(" ", strip=True) if ref_el else ""
    m_ref = re.search(r"R[ée]f\s*:?\s*([A-Za-z0-9]+)", ref_txt)
    ref = m_ref.group(1) if m_ref else ""
    # id numérique du slug final en secours
    id_num = ""
    if parts:
        m = re.match(r"^(\d+)-", parts[-1])
        if m:
            id_num = m.group(1)
    id_annonce = ref or id_num or url

    # Titre brut : "Ville (CODEPOSTAL) Reste du titre…"
    title_el = card.select_one(".item__title")
    titre_brut = title_el.get_text(" ", strip=True) if title_el else ""
    ville, code_postal, titre = _parse_title(titre_brut)
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Prix
    price_el = card.select_one(".item__price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Options : pièces / chambres
    opts_el = card.select_one(".item__options")
    opts_text = opts_el.get_text(" ", strip=True) if opts_el else ""
    pieces = _parse_int(r"(\d+)\s*Pi[eè]ce", opts_text)
    chambres = _parse_int(r"(\d+)\s*Chambre", opts_text)
    # Pièces en secours : segment tN de l'URL
    if pieces is None and len(parts) > 4:
        m = re.match(r"^t(\d+)$", parts[4])
        if m:
            pieces = int(m.group(1))

    # Surface habitable : extraite du titre ("… 208m² …")
    surface = _parse_surface_hab(titre_brut)

    # Photos
    photos = []
    for img in card.select("img.item__img, .item__visual img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "winkey",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Winkey Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_title(text: str) -> tuple[str, str, str]:
    """'Le Mans (72100) Maison familiale 208m²' → ('Le Mans', '72100', 'Maison familiale 208m²')"""
    if not text:
        return "", "", ""
    m = re.match(r"\s*(.+?)\s*\((\d{5})\)\s*(.*)$", text)
    if m:
        return m.group(1).strip(), m.group(2), m.group(3).strip()
    # pas de motif "Ville (CP)" → tout en titre
    return "", "", text.strip()


def _parse_surface_hab(text: str) -> float | None:
    """Cherche 'NNN m²' / 'NNNm²' dans le titre."""
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0]*)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 2000:
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
    print(f"\nTotal Winkey: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p/{b['chambres'] or '?'}ch"
            f" — {b['type_bien']} — {b['ville']}"
        )

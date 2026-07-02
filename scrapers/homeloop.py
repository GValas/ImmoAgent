"""scrapers/homeloop.py — Homeloop (iBuyer : achat-revente immédiat)

Méthode : scrape_simple (httpx) — SSR HTML (Laravel Blade, pas de JS).
URL pattern : /fr/immobilier/vente?departements=NN
              → filtre département CÔTÉ SERVEUR (vérifié : aucune fuite hors-dept ;
                33→33 only, 75→75 only, 28→28 only, 89→89 only).
              Pas de pagination paramétrique observée (page=2 → 0 carte) ; la liste
              par département tient sur une seule page (inventaire iBuyer réduit).

Cartes : div.card.property-card
  - URL   : a.link[href] → /fr/immobilier/vente/{type}/{ville-slug}/{titre-slug}/{id}
  - Feat  : p.card-features → "N pièces • NNN m² • Ville CODEPOSTAL"
  - Titre : p.card-title    → "Maison à vendre à Écrosnes - 173m² 5 pièces"
  - Prix  : p.card-price    → "300 000 €"
  - Photo : picture source[srcset] / img.photo[src] (CloudFront webp)

Type de bien : déduit du segment d'URL (maison / appartement / terrain). On ne
               garde que maisons / propriétés (terrain & appartement exclus).

Couverture : iBuyer urbain — l'inventaire est concentré sur les grandes métropoles
             (33, 75, 92, 67, 44, 06, 83…). Sur les départements cibles du Val-de-
             Loire / Ouest, le stock est faible mais réel (ex. 28 : ~6 biens,
             89 : ~1 bien ; 72/45 : 0 au dernier test).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.homeloop.fr"
LIST_PATH = "/fr/immobilier/vente"
PHOTOS_PER_CARD = 10


# Types de bien (segment d'URL) à conserver : maisons / propriétés.
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|chalet",
    re.IGNORECASE,
)
# Types explicitement exclus.
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
            try:
                biens = await _scrape_dept(
                    client, dept, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[Homeloop] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Homeloop] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    url = f"{BASE_URL}{LIST_PATH}?departements={dept}"
    r = await client.get(url)
    if r.status_code != 200:
        return biens

    cards = BeautifulSoup(r.text, "html.parser").select("div.property-card")
    for card in cards:
        try:
            bien = _parse_card(card, dept)
        except Exception:
            continue
        if not bien:
            continue

        # Sécurité : on n'accepte que le département cible (filtre serveur déjà OK).
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

    return biens


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a.link") or card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type & id depuis l'URL : /fr/immobilier/vente/{type}/{ville}/{titre-slug}/{id}
    parts = [p for p in href.split("/") if p]
    # parts ≈ ['fr','immobilier','vente','maison','ecrosnes','titre-slug','85154']
    type_seg = parts[3] if len(parts) > 3 else ""
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg.replace("-", " ").strip() or "maison"

    id_annonce = parts[-1] if parts and parts[-1].isdigit() else url

    # Features : "N pièces • NNN m² • Ville CODEPOSTAL"
    feat_el = card.select_one(".card-features")
    feat = feat_el.get_text(" ", strip=True) if feat_el else ""
    feat = re.sub(r"\s+", " ", feat)

    pieces = _parse_int(r"(\d+)\s*pi[eè]ces?", feat)
    surface = _parse_surface(feat)
    ville, code_postal = _parse_loc(feat)

    # Titre
    title_el = card.select_one(".card-title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} à vendre à {ville}".strip()
    # Secours surface depuis le titre si absente des features
    if surface is None:
        surface = _parse_surface(titre)

    # Prix
    price_el = card.select_one(".card-price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Photos (CloudFront webp)
    photos: list[str] = []
    for src in card.select("picture source"):
        s = src.get("srcset", "").split(",")[0].strip().split(" ")[0]
        if s and not s.startswith("data:"):
            photos.append(s)
    img = card.select_one("img.photo") or card.select_one("img")
    if img:
        s = img.get("src", "")
        if s and not s.startswith("data:"):
            photos.append(s)
    # dédup en conservant l'ordre
    photos = list(dict.fromkeys(photos))[:PHOTOS_PER_CARD]

    return {
        "source": "homeloop",
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
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Homeloop",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'5 pièces • 173 m² • Écrosnes 28320' → ('Écrosnes', '28320')"""
    cp = ""
    m_cp = re.search(r"\b(\d{5})\b", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = ""
    # Dernier segment après le dernier '•' : "Ville CODEPOSTAL"
    last = text.split("•")[-1].strip()
    last = re.sub(r"\b\d{5}\b", "", last).strip(" -•")
    if last:
        ville = last
    return ville, cp


def _parse_surface(text: str) -> float | None:
    """'173 m²' ou '- 173m²' → 173.0"""
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0]*)\s*m²", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 5000:
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
    print(f"\nTotal Homeloop: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

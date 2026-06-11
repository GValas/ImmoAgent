"""scrapers/officeduviager.py — Office du Viager (annonces de viager)

Méthode : scrape_simple (httpx) — SSR HTML (Symfony, contenu dans le HTML brut).
URL pattern : /annonces/viager-{dept-slug}   (ex: /annonces/viager-loiret)
              → filtre département CÔTÉ SERVEUR (vérifié : chaque page ne renvoie
              que des biens du département demandé, aucune fuite hors-dept).

Cartes : div.product-miniature
  - URL    : a.product-name[href]  → /annonces/{ville-cp}/{slug-ref}
  - Titre  : a.product-name[title]  →  "Maison - 7 pièces - 160 m²"
             (type de bien, nb de pièces, surface habitable y sont encodés)
  - Loc    : .heading .name (la dernière)  →  "45220 Triguères"
  - Prix   : .features-highlight li avec "Valeur du bien"  →  "320 000 €"
             (= valeur vénale du bien ; le bouquet / la rente sont mis en description)
  - Bouquet/Rente/Décote/Référence : .features-highlight li (name → value)
  - Photo  : .img-product-miniature[style=background-image:url(...)]

Particularités : 100 % viager (occupé ou libre). On ne garde que les biens de
type maison/propriété (le portail liste surtout des maisons et appartements).
Le "prix" renseigné est la VALEUR VÉNALE (pas le bouquet) pour rester comparable
aux autres sources. Bouquet et rente sont reportés en description.

Couverture : présence inégale ; sur les départements cibles l'inventaire est
faible mais réel (45, 37, 36, 18, 41, 72, 28, 58 ont des biens ; 89/49/53 = 0).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.officeduviager.fr"
PHOTOS_PER_CARD = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Code département → slug URL /annonces/viager-{slug}
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

# Types de bien conservés (déduits du titre / slug)
_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|long[eè]re|manoir|chateau|ch[aâ]teau|"
    r"moulin|demeure|domaine|mas|g[iî]te|corps[- ]de[- ]ferme|pavillon",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|studio",
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
                print(f"[OfficeViager] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[OfficeViager] Erreur dept {dept}: {e}")
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

    url = f"{BASE_URL}/annonces/viager-{slug}"
    r = await client.get(url)
    if r.status_code != 200:
        return biens

    cards = BeautifulSoup(r.text, "html.parser").select(".product-miniature")
    for card in cards:
        try:
            bien = _parse_card(card, dept)
        except Exception:
            continue
        if not bien:
            continue

        # Sécurité : filtre département strict (le filtre serveur est déjà OK)
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
    link = card.select_one("a.product-name")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    titre = (link.get("title") or link.get_text(" ", strip=True)).strip()

    # Type de bien (depuis le titre + slug)
    type_src = f"{titre} {href}"
    if _EXCLUDE_TYPE.search(type_src) and not _KEEP_TYPE.search(titre):
        return None
    m_type = _KEEP_TYPE.search(titre)
    if not m_type:
        # type ambigu / non maison → on exclut par prudence
        return None
    type_bien = m_type.group(0).lower()

    # Localisation : la dernière span.name du heading porte "45220 Triguères"
    ville, code_postal = "", ""
    for nm in card.select(".heading .name"):
        txt = nm.get_text(" ", strip=True)
        m = re.search(r"(\d{5})\s+(.+)$", txt)
        if m:
            code_postal = m.group(1)
            ville = m.group(2).strip()
            break
    # secours : CP depuis le slug d'URL (.../ville-45220/...)
    if not code_postal:
        m = re.search(r"-(\d{5})/", href) or re.search(r"-(\d{5})", href)
        if m:
            code_postal = m.group(1)

    # Pièces & surface depuis le titre "Maison - 7 pièces - 160 m²"
    pieces = _parse_int(r"(\d+)\s*pi[eè]ces?", titre)
    surface = _parse_surface(titre)

    # Features : valeur du bien (prix), bouquet, rente, décote, référence
    feats = _parse_features(card)
    prix = _parse_price(feats.get("valeur du bien", ""))
    ref = feats.get("référence", "") or feats.get("reference", "")
    id_annonce = ref or _slug_id(href) or url

    # Description : on consolide les infos viager (utile au match qualitatif)
    desc_parts = []
    flag = card.select_one(".flag")
    if flag:
        desc_parts.append(flag.get_text(" ", strip=True))
    for k in ("bouquet", "rente mensuelle", "décote", "decote"):
        if feats.get(k):
            desc_parts.append(f"{k.title()} : {feats[k]}")
    profile = card.select_one(".profile")
    if profile:
        desc_parts.append("Crédirentier(s) : " + profile.get_text(" ", strip=True))
    description = " — ".join(desc_parts)

    # Photo (background-image du visuel)
    photos = []
    img = card.select_one(".img-product-miniature")
    if img and img.get("style"):
        m = re.search(r"url\(['\"]?([^'\")]+)", img["style"])
        if m:
            src = m.group(1)
            if src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "officeduviager",
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
        "agence": "Office du Viager",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_features(card) -> dict:
    """Retourne {name_lower: value} pour les li de .features-highlight."""
    out: dict[str, str] = {}
    for li in card.select(".features-highlight li.feature"):
        name = li.select_one(".name")
        val = li.select_one(".value")
        if name and val:
            out[name.get_text(" ", strip=True).lower()] = val.get_text(" ", strip=True)
    return out


def _slug_id(href: str) -> str:
    m = re.search(r"(\d{4}-\d+)\b", href)
    return m.group(1) if m else ""


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_surface(text: str) -> float | None:
    # Gère "160 m²", "95,35 m²", "185,80 m²", "1 200 m²"
    m = re.search(r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
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
    print(f"\nTotal Office du Viager: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

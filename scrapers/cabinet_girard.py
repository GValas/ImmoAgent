"""scrapers/cabinet_girard.py — Cabinet Girard Immobilier (agence de Nevers, 58)

Méthode : scrape_simple (httpx) — SSR HTML (plateforme Altera/Periscope).
Site : https://cabinet-girard-immobilier.fr  — petite agence de la préfecture
       de la Nièvre (Nevers). Inventaire concentré sur le 58 et les communes
       limitrophes (Cher 18, etc.).

URL liste : /fr/ventes?page=N   (pagination ?page=N, ~9 biens/page).
Cartes (page liste) :  li.property[data-property-id]
  - id    : data-property-id
  - URL   : a[href]  → /fr/propriété/{id}  (href URL-encodé)
  - Ville : h3                    (la liste n'expose PAS de code postal)
  - Type+prix : p  → "Maison" + span.price "139 000 €"
  - Surface/Terrain : li.area  → "151 m²" (ambigu liste ; affiné en page détail)

Filtre département : le site n'expose AUCUN code postal sur la carte ni la page
détail (seule l'adresse de l'AGENCE — 58000 — y figure). On résout donc le NOM
DE COMMUNE de chaque annonce en (codeDepartement, codePostal) via l'API officielle
geo.api.gouv.fr (match exact de nom prioritaire), puis post-filtre STRICT
code_postal[:2] ∈ départements cibles. Aucune fuite hors-zone possible.

Page détail (enrichissement surface/pièces/DPE/description) : li libellés
  « Pièces 7 pièces », « Surface 151 m² » (Loi Carrez = habitable), « N Chambres »,
  DPE « classe X » ; description dans le premier <p> long.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import unicodedata

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://cabinet-girard-immobilier.fr"
GEO_API = "https://geo.api.gouv.fr/communes"
MAX_PAGES = 8
PHOTOS_PER_CARD = 12


# Types conservés (maisons / propriétés) vs exclus (appart, garage, immeuble...)
_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|maison de ville",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"viager",
    re.IGNORECASE,
)


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()
    geo_cache: dict[str, tuple[str, str]] = {}

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/fr/ventes" + (f"?page={page}" if page > 1 else "")
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[CabinetGirard] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("li.property")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                try:
                    base = _parse_card(card)
                except Exception:
                    continue
                if not base:
                    continue
                if base["id_annonce"] in seen_ids:
                    continue

                # Résolution département via API officielle (filtre STRICT)
                dept, cp = await _resolve_dept(client, base["ville"], geo_cache)
                if not dept or dept not in departements:
                    continue
                base["departement"] = dept
                base["code_postal"] = cp

                # Bornes prix sur la valeur de la liste (sûre)
                p = base.get("prix") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue

                # Enrichissement page détail (surface habitable, pièces, DPE, desc.)
                try:
                    await _enrich_detail(client, base)
                except Exception:
                    pass

                s = base.get("surface") or 0
                if surface_min and s and s < surface_min:
                    continue

                seen_ids.add(base["id_annonce"])
                results.append(base)
                new_on_page += 1
                await asyncio.sleep(0.4)

            if new_on_page == 0 and page > 1:
                # plus rien de nouveau (probable fin de pagination)
                pass
            await asyncio.sleep(0.5)

    print(f"[CabinetGirard] Total retenu : {len(results)} annonces")
    return results


def _parse_card(card) -> dict | None:
    pid = card.get("data-property-id") or ""
    link = card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not pid or not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type + ville + prix
    h3 = card.select_one("h3")
    ville = h3.get_text(" ", strip=True) if h3 else ""

    p_el = card.select_one(".content p") or card.select_one("p")
    type_raw = ""
    if p_el:
        # le texte du <p> = "Maison\n139 000 €" → 1ère ligne = type
        type_raw = p_el.get_text("\n", strip=True).split("\n")[0]
    type_bien = type_raw.strip() or "maison"
    if _EXCLUDE_TYPE.search(type_bien) and not _KEEP_TYPE.search(type_bien):
        return None
    if not _KEEP_TYPE.search(type_bien):
        return None

    price_el = card.select_one(".price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Surface/terrain en liste (ambigu) — affiné en page détail
    area_el = card.select_one(".area")
    area_val = _parse_area(area_el.get_text(" ", strip=True) if area_el else "")

    img = card.select_one("img")
    photos = []
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            photos.append(src)

    titre = f"{type_bien} {ville}".strip()

    return {
        "source": "cabinet_girard",
        "url": url,
        "id_annonce": pid,
        "titre": titre[:150],
        "type_bien": type_bien.lower(),
        "description": "",
        "departement": "",          # rempli après résolution geo
        "ville": ville[:80],
        "code_postal": "",          # rempli après résolution geo
        "surface": area_val,        # provisoire (peut être terrain) → corrigé en détail
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Cabinet Girard Immobilier",
    }


async def _resolve_dept(
    client: httpx.AsyncClient, ville: str, cache: dict[str, tuple[str, str]]
) -> tuple[str, str]:
    """Nom de commune → (codeDepartement, codePostal) via geo.api.gouv.fr.

    Match exact de nom prioritaire (insensible casse/accents), repli sur le
    1er résultat trié par population. Renvoie ("", "") si non résolu.
    """
    key = _strip_accents(ville).lower().strip()
    if not key:
        return "", ""
    if key in cache:
        return cache[key]

    res: tuple[str, str] = ("", "")
    try:
        r = await client.get(
            GEO_API,
            params={
                "nom": ville,
                "fields": "nom,codesPostaux,codeDepartement",
                "boost": "population",
                "limit": 5,
            },
        )
        if r.status_code == 200:
            data = r.json()
            chosen = None
            for c in data:
                if _strip_accents(c.get("nom", "")).lower() == key:
                    chosen = c
                    break
            if chosen is None and data:
                chosen = data[0]
            if chosen:
                dept = chosen.get("codeDepartement", "") or ""
                cps = chosen.get("codesPostaux", []) or []
                cp = cps[0] if cps else (dept + "000" if dept else "")
                res = (dept, cp)
    except Exception:
        res = ("", "")

    cache[key] = res
    return res


async def _enrich_detail(client: httpx.AsyncClient, bien: dict) -> None:
    """Récupère surface habitable, pièces, chambres, DPE, description, photos."""
    url = bien["url"]
    # href peut contenir des caractères accentués déjà encodés ; on requête tel quel
    r = await client.get(url)
    if r.status_code != 200:
        return
    soup = BeautifulSoup(r.text, "html.parser")
    full_txt = soup.get_text(" ", strip=True)

    # li libellés "Surface 151 m²", "Pièces 7 pièces"
    surface = None
    pieces = None
    chambres = 0
    for li in soup.select("li"):
        t = li.get_text(" ", strip=True)
        if surface is None:
            m = re.search(r"Surface\s+([\d.,\s\xa0]+)\s*m", t, re.IGNORECASE)
            if m:
                surface = _to_float(m.group(1))
        if pieces is None:
            m = re.search(r"Pi[eè]ces\s+(\d+)", t, re.IGNORECASE)
            if m:
                pieces = int(m.group(1))
        m = re.search(r"(\d+)\s+Chambres?", t, re.IGNORECASE)
        if m:
            chambres += int(m.group(1))

    # Loi Carrez en secours pour la surface habitable
    if surface is None:
        m = re.search(r"Loi\s+Carrez\s+([\d.,\s\xa0]+)\s*m", full_txt, re.IGNORECASE)
        if m:
            surface = _to_float(m.group(1))

    if surface:
        bien["surface"] = surface
    if pieces:
        bien["pieces"] = pieces
    if chambres:
        bien["chambres"] = chambres

    # Terrain (souvent absent ; parfois dans le texte)
    m = re.search(r"[Tt]errain[^0-9]{0,20}([\d\s\xa0]{2,8})\s*m", full_txt)
    if m:
        bien["surface_terrain"] = _to_float(m.group(1))

    # DPE : "classe X"
    m = re.search(r"classe\s+([A-G])\b", full_txt)
    if m:
        bien["dpe"] = m.group(1).upper()

    # Description : premier <p> long
    for p in soup.select("p"):
        t = p.get_text(" ", strip=True)
        if len(t) > 100:
            bien["description"] = t[:1500]
            break

    # Photos (galerie cloudfront)
    photos = []
    for img in soup.select("img"):
        src = img.get("data-src") or img.get("src") or ""
        if "cloudfront" in src and not src.startswith("data:"):
            if src not in photos:
                photos.append(src)
    if photos:
        bien["photos"] = photos[:PHOTOS_PER_CARD]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_area(text: str) -> float | None:
    m = re.search(r"([\d.,\s\xa0]+)\s*m", text)
    return _to_float(m.group(1)) if m else None


def _to_float(raw: str) -> float | None:
    raw = re.sub(r"[\s\xa0]", "", raw).replace(",", ".")
    raw = re.sub(r"[^\d.]", "", raw)
    if raw.count(".") > 1:
        raw = raw.replace(".", "", raw.count(".") - 1)
    try:
        return float(raw) if raw else None
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
    print(f"\nTotal Cabinet Girard: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}"
            f" — {b.get('pieces') or '?'}p — {b['type_bien']} — {b['ville']}"
        )

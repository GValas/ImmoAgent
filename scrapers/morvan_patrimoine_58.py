"""scrapers/morvan_patrimoine_58.py — Morvan Patrimoine (agence Château-Chinon / St-Saulge, Nièvre)

Méthode : scrape_simple (httpx) — SSR PHP, pas de Playwright.
Domaine RÉEL : https://www.morvan-patrimoine.com/  (avec tiret ; "morvanpatrimoine.com"
               sans tiret ne résout pas en DNS). Distinct de morvan-immobilier.com (Lormes).

Stratégie :
  1. Listing SSR  : /vente.php  → contient les cartes (div.resultat) avec un lien
     vers chaque fiche détail : /fiche-{type}-a-vendre-{ville}-ref-{N}.php
     (La pseudo-pagination ?Page=N est purement décorative côté serveur : elle
      renvoie toujours les mêmes 12 fiches → on ne pagine pas, on dédoublonne.)
  2. Fiche détail : JSON-LD `RealEstateListing` propre, qui porte toutes les
     données structurées du BIEN (pas de l'agence) :
        offers.price                          → prix
        offers.itemOffered.floorSize.value    → surface habitable (m²)
        offers.itemOffered.numberOfRooms      → pièces
        offers.itemOffered.numberOfBedrooms   → chambres
        offers.itemOffered.address.postalCode → code postal DU BIEN (≠ agence)
        offers.itemOffered.address.addressLocality → ville
        offers.itemOffered.image[]            → photos
        description                           → description complète
     Terrain : pas de champ dédié → extrait du texte ("terrain de N m²").
     DPE     : lettre A–G repérée dans le HTML détail si présente.

Filtre département : agence implantée en Nièvre (58) et Saône-et-Loire (71).
  → POST-FILTRE STRICT sur le code postal DU BIEN (offers.itemOffered.address.postalCode)
    contre la liste des départements cibles. 0 fuite hors-zone visé.
    (Le 71 n'étant pas une cible, il est rejeté ; au runtime tout le stock est en 58.)

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.morvan-patrimoine.com"
LISTING_URL = f"{BASE_URL}/vente.php"
MAX_PHOTOS = 12


_FICHE_RE = re.compile(r"fiche-[a-z0-9-]+-ref-(\d+)\.php", re.IGNORECASE)

# Types de bien à conserver (segment du nom de fiche : "fiche-maison-a-vendre-...")
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|corps-de-ferme|grange|pavillon",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|bar|tabac|hotel|restaurant|atelier|cave\b",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        try:
            fiches = await _collect_fiches(client)
        except Exception as e:
            print(f"[MorvanPatrimoine] Erreur listing : {e}")
            return results

        print(f"[MorvanPatrimoine] {len(fiches)} fiches trouvées sur le listing")

        seen_ref: set[str] = set()
        for ref, path in fiches:
            if ref in seen_ref:
                continue
            seen_ref.add(ref)
            try:
                bien = await _scrape_fiche(client, ref, path)
            except Exception as e:
                print(f"[MorvanPatrimoine] Erreur fiche {ref}: {e}")
                bien = None
            await asyncio.sleep(0.5)
            if not bien:
                continue

            # ── Post-filtre STRICT département (0 fuite) ──
            cp = bien.get("code_postal") or ""
            if not cp or cp[:2] not in departements:
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

    print(f"[MorvanPatrimoine] {len(results)} biens retenus (zone cible)")
    return results


async def _collect_fiches(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    """Renvoie [(ref, '/fiche-...-ref-N.php'), ...] uniques depuis le listing SSR."""
    r = await client.get(LISTING_URL)
    if r.status_code != 200:
        return []
    out: dict[str, str] = {}
    for m in _FICHE_RE.finditer(r.text):
        ref = m.group(1)
        path = "/" + m.group(0)
        # ne garder que les types maison/propriété d'après le slug
        slug = m.group(0)
        if _EXCLUDE_TYPE.search(slug) and not _KEEP_TYPE.search(slug):
            continue
        out.setdefault(ref, path)
    return list(out.items())


async def _scrape_fiche(
    client: httpx.AsyncClient, ref: str, path: str
) -> dict | None:
    url = BASE_URL + path
    r = await client.get(url)
    if r.status_code != 200:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    data = _extract_jsonld(soup)
    if not data:
        return None

    offers = data.get("offers") or {}
    item = offers.get("itemOffered") or {}
    addr = item.get("address") or {}

    code_postal = str(addr.get("postalCode") or "").strip()
    ville = (addr.get("addressLocality") or "").strip().title()

    # Type de bien depuis le slug de la fiche
    type_slug = path.split("fiche-", 1)[-1].split("-a-vendre", 1)[0]
    type_bien = type_slug.replace("-", " ").strip() or "maison"

    titre = (data.get("name") or item.get("name") or "").strip()
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    description = (data.get("description") or "").replace("\r", " ").replace("\n", " ")
    description = re.sub(r"\s+", " ", description).strip()

    prix = _to_float(offers.get("price"))
    if prix is None:
        prix = _parse_price_from_text(titre)

    surface = _to_float((item.get("floorSize") or {}).get("value"))
    pieces = _to_int(item.get("numberOfRooms"))
    chambres = _to_int(item.get("numberOfBedrooms"))

    surface_terrain = _parse_terrain(description)
    dpe = _parse_dpe(r.text)

    photos = _collect_photos(item)

    return {
        "source": "morvan_patrimoine_58",
        "url": url,
        "id_annonce": ref,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:MAX_PHOTOS],
        "dpe": dpe,
        "agence": "Morvan Patrimoine",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_jsonld(soup: BeautifulSoup) -> dict | None:
    for s in soup.find_all("script", type="application/ld+json"):
        if not s.string:
            continue
        if "RealEstateListing" not in s.string:
            continue
        try:
            d = json.loads(s.string)
        except Exception:
            continue
        if isinstance(d, list):
            d = next((x for x in d if x.get("@type") == "RealEstateListing"), None)
        if isinstance(d, dict) and d.get("@type") == "RealEstateListing":
            return d
    return None


def _collect_photos(item: dict) -> list[str]:
    imgs = item.get("image")
    if not imgs:
        return []
    if isinstance(imgs, str):
        imgs = [imgs]
    out: list[str] = []
    for src in imgs:
        if not isinstance(src, str) or not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = BASE_URL + src
        out.append(src)
    return out


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ".").replace("\xa0", "").replace(" ", ""))
    except (ValueError, TypeError):
        return None


def _to_int(v) -> int | None:
    f = _to_float(v)
    return int(f) if f is not None else None


def _parse_price_from_text(text: str) -> float | None:
    m = re.search(r"([\d\s\xa0]{3,})\s*(?:€|euros?)", text, re.IGNORECASE)
    if m:
        return _to_float(re.sub(r"[\s\xa0]", "", m.group(1)))
    return None


def _parse_terrain(text: str) -> float | None:
    """'terrain généreux de 1 560 m²' → 1560.0 (prend la plus grande mention)."""
    if not text:
        return None
    vals: list[float] = []
    for m in re.finditer(
        r"terrain[^.]{0,40}?([\d][\d\s\xa0]{1,7})\s*m", text, re.IGNORECASE
    ):
        v = _to_float(re.sub(r"[\s\xa0]", "", m.group(1)))
        if v and 10 <= v <= 1_000_000:
            vals.append(v)
    return max(vals) if vals else None


def _parse_dpe(html: str) -> str | None:
    m = re.search(
        r"(?:DPE|classe[ \-]?(?:énerg|energ)\w*|consommation\s+énerg\w*)"
        r"[^A-G:]{0,40}[:\s]([A-G])\b",
        html,
        re.IGNORECASE,
    )
    if m:
        letter = m.group(1).upper()
        if letter in "ABCDEFG":
            return letter
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
    print(f"\nTotal Morvan Patrimoine: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['pieces'] or '?'}p — DPE {b.get('dpe') or '?'} — {b['ville']}"
        )

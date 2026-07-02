"""scrapers/equestrian_immobilier.py — Equestrian Immobilier (spécialiste équestre)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress, thème RealHomes)

Site national spécialisé dans l'immobilier équestre (propriétés équestres,
haras, écuries, centres équestres avec terrain). Pas de filtre département
côté serveur : on scrape l'intégralité du catalogue (pagination
/les-proprietes-2/page/N/, ~6 cartes/page, une soixantaine de biens au total)
puis on POST-FILTRE strictement par département.

URL liste : https://www.equestrian-immobilier.com/les-proprietes-2/[page/N/]
Cartes liste : article.rh_list_card
  - URL/titre : h3 a[href]   (titre se termine souvent par "(NN)" = n° dept)
  - Prix      : .rh_list_card__price   →  "Achat 966,000€" / "1,190,000€"
  - Excerpt   : .rh_list_card__excerpt
  - Métas     : .rh_prop_card__meta  →  "Surface 216 m²", "Chambres 5"...
  - Photo     : .post_thumbnail[style="background: url('...')"]

Filtre département (STRICT, 0 fuite) :
  1. Pré-filtre rapide sur le n° de dept déduit du titre "(NN)" ou du slug
     d'URL terminant par "-NN/". Si ce n° n'est pas dans la zone cible → skip.
  2. Confirmation sur la PAGE DÉTAIL : le JSON-LD RealEstateListing expose
     address.streetAddress = "Ville, Nom-du-département, Région, ..., [CP,] France".
     On en extrait ville, code_postal (5 chiffres si présent) et le NOM du
     département. Le bien n'est conservé que si CP[:2] == dept OU si le nom de
     département de la zone cible apparaît dans streetAddress. Sinon → écarté.
  Aucun bien dont le département n'est pas confirmé dans la zone ne sort.

Type de bien : équestre (propriété équestre / haras / écurie). On conserve tout
le catalogue (maisons/propriétés à vocation équestre), le filtrage fin (surface,
prix) est appliqué quand les champs sont connus.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.equestrian-immobilier.com"
LIST_PATH = "/les-proprietes-2/"
MAX_PAGES = 12
PHOTOS_PER_CARD = 1  # la liste ne donne qu'une vignette ; gallery.py enrichira


# Code département → nom (tel qu'il apparaît dans streetAddress du JSON-LD).
# Sert au filtre strict quand le code postal est absent de l'adresse.
DEPT_NAMES: dict[str, str] = {
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


def _norm(s: str) -> str:
    """minuscule + suppression des accents pour comparaison de noms de dept."""
    s = s.lower()
    for a, b in (
        ("é", "e"), ("è", "e"), ("ê", "e"), ("ë", "e"),
        ("à", "a"), ("â", "a"), ("î", "i"), ("ï", "i"),
        ("ô", "o"), ("û", "u"), ("ù", "u"), ("ç", "c"),
    ):
        s = s.replace(a, b)
    return s


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    cibles = set(departements)
    noms_cibles = {DEPT_NAMES[d] for d in cibles if d in DEPT_NAMES}

    results: list[dict] = []
    seen_urls: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
    ) as client:
        # 1) Collecte de toutes les cartes du catalogue
        cards: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            path = LIST_PATH if page == 1 else f"{LIST_PATH}page/{page}/"
            url = BASE_URL + path
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Equestrian] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            page_cards = soup.select("article.rh_list_card")
            if not page_cards:
                break
            for card in page_cards:
                parsed = _parse_card(card)
                if parsed:
                    cards.append(parsed)
            await asyncio.sleep(0.5)

        print(f"[Equestrian] {len(cards)} biens au catalogue, confirmation dept…")

        # 2) Pré-filtre rapide par dept déduit du titre/URL
        candidats = []
        for c in cards:
            dguess = _dept_guess(c["titre"], c["url"])
            if dguess and dguess not in cibles:
                continue  # hors zone évident → skip
            candidats.append(c)

        # 3) Confirmation STRICTE sur la page détail (ville/CP/nom de dept)
        for c in candidats:
            if c["url"] in seen_urls:
                continue
            try:
                dept, ville, cp, description, terrain = await _confirm_detail(
                    client, c["url"], cibles, noms_cibles
                )
            except Exception as e:
                print(f"[Equestrian] détail KO {c['url'][-40:]}: {e}")
                continue
            if dept is None:
                continue  # département non confirmé dans la zone → on jette

            # filtres prix / surface (sans exclure les champs manquants)
            p = c.get("prix") or 0
            s = c.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            seen_urls.add(c["url"])
            results.append(
                {
                    "source": "equestrian_immobilier",
                    "url": c["url"],
                    "id_annonce": c["id_annonce"],
                    "titre": c["titre"][:150],
                    "type_bien": "propriete equestre",
                    "description": (description or c.get("excerpt") or "")[:1200],
                    "departement": dept,
                    "ville": (ville or "")[:80],
                    "code_postal": cp or "",
                    "surface": c.get("surface"),
                    "surface_terrain": terrain,
                    "pieces": None,
                    "chambres": c.get("chambres"),
                    "prix": c.get("prix"),
                    "photos": c.get("photos", []),
                    "dpe": None,
                    "agence": "Equestrian Immobilier",
                }
            )
            await asyncio.sleep(0.4)

    # garde-fou final : aucune fuite hors-zone
    results = [
        b for b in results
        if b["departement"] in cibles
        and (not b["code_postal"] or b["code_postal"][:2] in cibles)
    ]
    print(f"[Equestrian] {len(results)} biens retenus en zone cible")
    return results


# ── Parsing carte liste ───────────────────────────────────────────────────────

def _parse_card(card) -> dict | None:
    link = card.select_one("h3 a[href]")
    if not link:
        return None
    href = link.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href
    titre = link.get_text(" ", strip=True)

    # id_annonce : data-propertyid du bouton favori, sinon slug d'URL
    id_annonce = ""
    fav = card.select_one("[data-propertyid]")
    if fav:
        id_annonce = fav.get("data-propertyid", "")
    if not id_annonce:
        slug = [p for p in url.rstrip("/").split("/") if p]
        id_annonce = slug[-1] if slug else url

    # Prix : "Achat 966,000€" / "1,190,000€"
    price_el = card.select_one(".rh_list_card__price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Excerpt
    exc_el = card.select_one(".rh_list_card__excerpt")
    excerpt = exc_el.get_text(" ", strip=True) if exc_el else ""

    # Métas : Surface / Chambres
    surface = None
    chambres = None
    for m in card.select(".rh_prop_card__meta"):
        t = m.get_text(" ", strip=True)
        if re.search(r"surface", t, re.IGNORECASE):
            surface = _parse_surface(t)
        elif re.search(r"chambre", t, re.IGNORECASE):
            n = re.search(r"(\d+)", t)
            if n:
                chambres = int(n.group(1))

    # Photo (vignette de fond)
    photos = []
    fig = card.select_one(".post_thumbnail")
    if fig:
        style = fig.get("style", "")
        mimg = re.search(r"url\(['\"]?(https?://[^'\")]+)", style)
        if mimg:
            photos.append(mimg.group(1))
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre,
        "prix": prix,
        "surface": surface,
        "chambres": chambres,
        "excerpt": excerpt,
        "photos": photos,
    }


def _dept_guess(titre: str, url: str) -> str | None:
    """Numéro de dept déduit du titre '(NN)' ou du slug d'URL '-NN/'. Pré-filtre."""
    m = re.search(r"\((\d{2,3})\)\s*$", titre or "")
    if m:
        return m.group(1)[:2]
    m = re.search(r"-(\d{2,3})/?$", (url or "").rstrip("/"))
    if m:
        return m.group(1)[:2]
    return None


# ── Confirmation page détail (JSON-LD streetAddress) ──────────────────────────

async def _confirm_detail(
    client: httpx.AsyncClient,
    url: str,
    cibles: set[str],
    noms_cibles: set[str],
) -> tuple:
    """Retourne (dept, ville, cp, description, terrain) si le département est
    confirmé DANS la zone cible, sinon (None, None, None, None, None)."""
    r = await client.get(url)
    if r.status_code != 200:
        return (None, None, None, None, None)
    soup = BeautifulSoup(r.text, "html.parser")

    ld = None
    for sc in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(sc.get_text())
        except Exception:
            continue
        if isinstance(data, dict) and data.get("@type") == "RealEstateListing":
            ld = data
            break

    ville = ""
    cp = ""
    description = ""
    street = ""
    if ld:
        description = (ld.get("description") or "").strip()
        addr = ld.get("address") or {}
        street = addr.get("streetAddress", "") or ""

    # streetAddress = "Ville, Nom-dept, Région, France métropolitaine, [CP,] France"
    cp_m = re.search(r"\b(\d{5})\b", street)
    if cp_m:
        cp = cp_m.group(1)
    seg = [s.strip() for s in street.split(",") if s.strip()]
    if seg:
        ville = seg[0]
    street_norm = _norm(street)

    # Détermination STRICTE du département
    dept = None
    if cp and cp[:2] in cibles:
        dept = cp[:2]
    else:
        for code in cibles:
            nom = DEPT_NAMES.get(code)
            if nom and re.search(rf"\b{re.escape(nom)}\b", street_norm):
                dept = code
                break

    if dept is None:
        return (None, None, None, None, None)

    # Terrain : "NN hectares" / "NN ha" dans la description ou la page
    terrain = _parse_terrain_ha(description) or _parse_terrain_ha(
        soup.get_text(" ", strip=True)
    )

    return (dept, ville, cp, description, terrain)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    # "Achat 966,000€" / "1,190,000€"  → la virgule est le séparateur de milliers
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    if v and v < 1000:  # garde-fou : un prix immobilier crédible
        return None
    return v


def _parse_surface(text: str) -> float | None:
    m = re.search(r"(\d[\d\s\xa0]*)\s*m", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 5000:
                return f
        except ValueError:
            pass
    return None


def _parse_terrain_ha(text: str) -> float | None:
    """'15 hectares' / '18 ha' → m² (×10000). Renvoie la 1ʳᵉ mention crédible."""
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:hectares?|ha)\b", text, re.IGNORECASE)
    if m:
        try:
            ha = float(m.group(1).replace(",", "."))
            if 0 < ha <= 1000:
                return round(ha * 10000, 0)
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
    print(f"\nTotal Equestrian Immobilier: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens})
    print(f"Départements vus : {depts}")
    cp_depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements (via CP) : {cp_depts}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}|{b['code_postal'] or '?????'}] "
            f"{b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['ville']}"
        )

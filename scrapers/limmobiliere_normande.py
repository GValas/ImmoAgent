"""scrapers/limmobiliere_normande.py — L'Immobilière Normande (réseau ~10 agences)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress + thème tagDiv/Newspaper).
URL pattern : /acheter/?tdb-loop-page={N}
              → liste paginée unique (PAS de filtre département serveur).
              Le réseau couvre l'Eure (27) et un peu les Yvelines (78), sur l'axe
              Paris-Rouen → filtre département CÔTÉ CLIENT via le préfixe de la
              référence d'annonce (les 2 premiers chiffres du numéro de réf = code
              département), vérifié : 270xxxxx = Eure (27), 780xxxxx = Yvelines (78).

Cartes : div.td_module_wrap
  - URL    : a[href*="/annonce/"]  → /annonce/{REF}-{slug}/
             REF = {dept}{6 chiffres}, ex 270098648 → dept 27
  - Titre  : a[title] (sur le lien image)
  - Photo  : img.entry-thumb[src]
  - Champs : div.tdb_single_custom_field (ordre variable selon le type de bien) :
       .prixannonce            → prix (ex "96000")
       champ "VILLE (secteur)" → ville (PAS de code postal exposé)
       champ "N pièces"        → pièces
       champ "NNN m²"          → surface habitable
       champ "N chambre(s)"    → chambres
       (les terrains n'ont ni pièces ni chambres)

Particularités :
  - Aucun code postal dans la liste ni proprement dans la page détail → le
    département est dérivé du PRÉFIXE DE RÉFÉRENCE (fiable), et `code_postal`
    reste None (on remplit `departement`).
  - Post-filtre STRICT sur le préfixe de réf → 0 fuite hors-département.
  - Types : on ne garde que maisons / propriétés (terrains/appartements exclus).

Couverture : réseau mono-région (27 Eure + 78 Yvelines, axe Paris-Rouen).
             AUCUN des départements cibles Val-de-Loire (72/28/45/89/...) n'est
             couvert → sur la zone actuelle le scraper renvoie légitimement 0 bien.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.limmobilierenormande.com"
SEARCH_PATH = "/acheter/"
MAX_PAGES = 10
PHOTOS_PER_CARD = 1  # la liste n'expose qu'une vignette par carte


# Types de bien à conserver (maisons / propriétés) — déduits du titre.
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|g[iî]te|corps de ferme|pavillon|"
    r"chaumi[eè]re|fermette",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|terrain|local|commerce|garage|parking|immeuble|"
    r"bureau|fonds|loft|duplex|f\d\b|t\d\b",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)
    dept_set = set(departements)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}{SEARCH_PATH}?tdb-loop-page={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[ImmoNormande] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.td_module_wrap")
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

                dept = bien["departement"]
                # Post-filtre STRICT : on ne garde que les départements cibles.
                if dept not in dept_set:
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
                new_on_page += 1

            print(
                f"[ImmoNormande] Page {page}: {len(cards)} cartes,"
                f" {new_on_page} retenues (zone)"
            )
            await asyncio.sleep(0.6)

    # Comptage par département (visibilité fuite éventuelle)
    if results:
        by_dept: dict[str, int] = {}
        for b in results:
            by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
        print(f"[ImmoNormande] Total {len(results)} biens — par dept: {by_dept}")

    return results


def _parse_card(card) -> dict | None:
    link = card.select_one('a[href*="/annonce/"]')
    if not link:
        return None
    href = link.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Référence + département depuis l'URL : /annonce/{REF}-{slug}/
    m_ref = re.search(r"/annonce/(\d+)-", href)
    if not m_ref:
        return None
    ref = m_ref.group(1)
    if len(ref) < 2:
        return None
    dept = ref[:2]  # préfixe de réf = code département (fiable sur ce site)
    id_annonce = ref

    # Titre (sur le lien image ou un lien titre porteur de l'attribut title)
    titre = link.get("title", "") or ""
    if not titre:
        title_link = card.select_one("a[title]")
        titre = title_link.get("title", "") if title_link else ""
    titre = titre.strip()

    # Type de bien : déduit du titre
    type_bien = _detect_type(titre)
    if type_bien is None:
        return None  # terrain / appartement / type non maison → exclu

    # Champs personnalisés (ordre variable selon le type de bien)
    prix = None
    ville = ""
    surface = None
    pieces = None
    chambres = None

    price_el = card.select_one(".prixannonce")
    if price_el:
        prix = _parse_price(price_el.get_text(" ", strip=True))

    for f in card.select(".tdb_single_custom_field"):
        txt = f.get_text(" ", strip=True)
        if not txt:
            continue
        low = txt.lower()
        if "(secteur)" in low or (low and low[0].isalpha() and "pièce" not in low
                                  and "chambre" not in low and "m²" not in low
                                  and txt != "exclu" and prix is not None):
            # champ localité : "VILLE (secteur)"
            cand = re.sub(r"\s*\(secteur\)\s*$", "", txt, flags=re.IGNORECASE).strip()
            if cand and cand.lower() != "exclu" and not ville:
                ville = cand
            continue
        if "pièce" in low or "piece" in low:
            mp = re.search(r"(\d+)", txt)
            if mp:
                pieces = int(mp.group(1))
            continue
        if "chambre" in low:
            mc = re.search(r"(\d+)", txt)
            if mc:
                chambres = int(mc.group(1))
            continue
        if "m²" in low or " m2" in low:
            surface = _parse_surface(txt)
            continue

    # Secours surface depuis le titre ("76m²", "127 m²")
    if surface is None:
        surface = _parse_surface(titre)

    # Photo (vignette unique)
    photos = []
    img = card.select_one("img.entry-thumb") or card.select_one("img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    return {
        "source": "limmobiliere_normande",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": None,  # non exposé par le site
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "L'Immobilière Normande",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _detect_type(titre: str) -> str | None:
    """Renvoie un libellé de type si maison/propriété, sinon None (exclu)."""
    if not titre:
        return None
    if _KEEP_TYPE.search(titre):
        # libellé condensé depuis le mot-clé reconnu
        m = _KEEP_TYPE.search(titre)
        return m.group(0).lower()
    if _EXCLUDE_TYPE.search(titre):
        return None
    # Titre ambigu (ex. "Coeur de Charleval ...") → exclu par prudence
    return None


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text).replace(",", ".")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'76 m²' / '76m²' / '161.28 m²' → float (surface habitable)."""
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m[²2]", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
        try:
            f = float(val)
            if 5 <= f <= 5000:
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
    print(f"\nTotal Immobilière Normande: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

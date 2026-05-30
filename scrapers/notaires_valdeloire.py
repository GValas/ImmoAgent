"""scrapers/notaires_valdeloire.py — Chambre interdép. des notaires du Val de Loire

Méthode : scrape_simple (httpx) — SSR HTML (portail notarial régional, technologie Novius).
URL pattern : /petites-annonces?typeBiens[]=MAI&typeTransactions[]=VENTE&page=N
  - SSR pur (httpx suffit, pas de Playwright) ; 12 cartes / page, pagination ?page=N.
  - Filtre TYPE de bien côté serveur via typeBiens[] (MAI=maison, AGR=propriété agricole…)
    et typeTransactions[]=VENTE (exclut location / viager / enchères).

Filtre DÉPARTEMENT : le portail couvre nativement 45 / 41 / 37 (+ qq fuites voisines
  rares : 77, 86…). Il n'expose PAS de paramètre département dans le formulaire — seul un
  filtre par commune existe (city[]=NOM_COMMUNE, peu pratique pour balayer un dept entier).
  → On scrape l'inventaire complet (MAI puis AGR) et on POST-FILTRE par département à
    partir du slug d'URL de chaque fiche (…/{ville}-{NN}/{id}) et du code postal extrait
    de la description. Aucune fuite hors-département (contrôle code_postal[:2] / slug NN).

Cartes : a.offer-card[href -> www.immobilier.notaires.fr/fr/annonce-immo/vente/{type}/{ville}-{NN}/{id}]
  - Date      : 1er <p class="text-gray-600">  (JJ/MM/AAAA)
  - Prix FAI  : <p ...notaires-color2-color> "348000 €"  (prix charge acquéreur, frais inclus)
  - Prix net  : span "Dont prix de vente : 330000 €"  (on garde le prix FAI comme `prix`)
  - Type      : <p> "Vente - Maison / villa"
  - Loc       : <p> "Dadonville - Loiret (45)"
  - Surf/pcs  : <p> "261m2 - 7 pièces"
  - Desc      : dernier <p class="text-2xs"> "Commune de DADONVILLE (45300)<br>…"  → CP exact
  - Photo     : img.image[src]

Couverture (mai 2026) : ~1150 maisons VENTE sur 96 pages, quasi exclusivement 45/41/37.
Les autres départements cibles (72, 28, 89, 49, 36, 18, 58, 53) → 0 stock natif (hors zone).
Complémentaire de `immobilier_notaires.py` (API nationale) : ici fiches notariales locales.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://chambre-interdep-valdeloire.notaires.fr"
LISTING_PATH = "/petites-annonces"
PER_PAGE = 12
MAX_PAGES = 110  # garde-fou : ~96 pages MAI + qq pages AGR

# Départements où ce portail a effectivement du stock (observé mai 2026).
# Cœur de zone : 45 / 41 / 37. Présence marginale (offices limitrophes) : 36/18/28/72/89.
COVERED_DEPTS = {"45", "41", "37", "36", "18", "28", "72", "89", "58", "49", "53"}

# Types de bien (typeBiens[]) à demander côté serveur : maisons + propriétés agricoles.
# (APP=appartement, TER=terrain, GAR=garage, COM/IMM/DIV/LAC/VIG exclus.)
TYPE_BIENS = ["MAI", "AGR"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# On ne retient que maisons / propriétés ; on rejette explicitement les types non-bâtis/divers.
_KEEP_TYPE = re.compile(
    r"maison|villa|propri[ée]t[ée]|ferme|longere|longère|manoir|chateau|ch[âa]teau|"
    r"moulin|demeure|domaine|mas|gite|gîte|corps de ferme|maison de",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|garage|parking|immeuble|local|commerce|bureau|fonds|"
    r"cave|box|viager",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Si aucun dept cible n'est couvert par le portail, inutile de scraper.
    targets = departements & COVERED_DEPTS
    if not targets:
        print(
            f"[NotairesValDeLoire] aucun dept cible dans la zone couverte "
            f"{sorted(COVERED_DEPTS)} → skip"
        )
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            params = [("typeTransactions[]", "VENTE")]
            params += [("typeBiens[]", t) for t in TYPE_BIENS]
            params.append(("page", str(page)))
            try:
                r = await client.get(BASE_URL + LISTING_PATH, params=params)
            except Exception as e:
                print(f"[NotairesValDeLoire] ERR page {page}: {e}")
                break
            if r.status_code != 200:
                break

            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select("a.offer-card")
            if not cards:
                break

            kept_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                dept = bien["departement"]
                # POST-FILTRE département : 0 fuite garantie.
                if dept not in departements:
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
                kept_on_page += 1

            # Page vide de cartes exploitables = on continue (les fuites hors-zone
            # peuvent occuper une page), mais on s'arrête s'il n'y a plus de carte du tout.
            await asyncio.sleep(0.4)

    # Bilan par dept
    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print(f"[NotairesValDeLoire] total: {len(results)} biens — par dept: {by_dept}")
    return results


def _parse_card(card) -> dict | None:
    href = card.get("href", "")
    if not href or "annonce-immo" not in href:
        return None
    url = href if href.startswith("http") else "https://www.immobilier.notaires.fr" + href

    # Département + id depuis le slug : …/{type}/{ville}-{NN}/{id}
    m_slug = re.search(r"/([a-z0-9\-]+)-(\d{2,3})/(\d+)\s*$", href)
    dept = ""
    id_annonce = ""
    if m_slug:
        dept = m_slug.group(2)[:2]
        id_annonce = m_slug.group(3)
    else:
        m_id = re.search(r"/(\d+)\s*$", href)
        id_annonce = m_id.group(1) if m_id else url

    # Type de bien : <p> "Vente - Maison / villa"
    type_bien = ""
    for p in card.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if re.match(r"^\s*Vente\b", txt) and "-" in txt:
            type_bien = re.sub(r"^\s*Vente\s*-\s*", "", txt).strip()
            break
    if not type_bien:
        # déduit du slug d'URL
        seg = href.split("/annonce-immo/vente/")
        if len(seg) > 1:
            type_bien = seg[1].split("/")[0].replace("-", " ")
    if _EXCLUDE_TYPE.search(type_bien) and not _KEEP_TYPE.search(type_bien):
        return None
    if type_bien and not _KEEP_TYPE.search(type_bien):
        return None
    type_bien = type_bien.strip() or "maison"

    # Localisation : <p> "Dadonville - Loiret (45)"
    ville = ""
    loc_dept = ""
    for p in card.find_all("p"):
        txt = p.get_text(" ", strip=True)
        m = re.match(r"^(.+?)\s*-\s*[\w'\-éèêàâ ]+\((\d{2,3})\)\s*$", txt)
        if m and "Maison" not in txt and "Vente" not in txt:
            ville = m.group(1).strip()
            loc_dept = m.group(2)[:2]
            break
    if not dept and loc_dept:
        dept = loc_dept

    # Surface + pièces : <p> "261m2 - 7 pièces"
    surface = None
    pieces = None
    full_text = card.get_text(" ", strip=True)
    m_surf = re.search(r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m2\b", full_text)
    if m_surf:
        try:
            surface = float(
                re.sub(r"[\s\xa0]", "", m_surf.group(1)).replace(",", ".")
            )
        except ValueError:
            surface = None
    m_pc = re.search(r"(\d+)\s*pi[eè]ces?", full_text)
    if m_pc:
        pieces = int(m_pc.group(1))

    # Code postal exact depuis la description : "Commune de DADONVILLE (45300)"
    code_postal = ""
    m_cp = re.search(r"\((\d{5})\)", full_text)
    if m_cp:
        code_postal = m_cp.group(1)
        if not dept:
            dept = code_postal[:2]
    elif dept and ville:
        code_postal = ""  # inconnu, dept suffit pour le filtre

    if not dept:
        return None

    # Prix : 1er <p notaires-color2-color> "348000 €" (FAI / charge acquéreur)
    prix = None
    price_p = card.select_one("p.notaires-color2-color")
    if price_p:
        prix = _parse_price(price_p.get_text(" ", strip=True))
    if prix is None:
        m_pr = re.search(r"([\d][\d\s\xa0]{2,})\s*€", full_text)
        if m_pr:
            prix = _parse_price(m_pr.group(0))

    # Description (dernier petit paragraphe)
    desc_p = card.select_one("p.text-2xs")
    description = ""
    if desc_p:
        description = desc_p.get_text(" ", strip=True).replace("<br>", " ")
    description = re.sub(r"&lt;br&gt;|<br\s*/?>", " ", description)

    # Photo
    photos = []
    img = card.select_one("img.image") or card.find("img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)

    titre = f"{type_bien.title()} à {ville}".strip() if ville else type_bien.title()

    return {
        "source": "notaires_valdeloire",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien[:60],
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
        "agence": "Notaires Val de Loire",
    }


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text)
    if not cleaned:
        return None
    try:
        val = float(cleaned)
        return val if val >= 1000 else None
    except ValueError:
        return None


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

    async def _test():
        depts = ["72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"]
        biens = await search(
            {
                "departements": depts,
                "prix_max": 0,
                "prix_min": 0,
                "surface_min": 0,
            }
        )
        print(f"\nTotal Notaires Val de Loire: {len(biens)} biens")
        by_dept: dict[str, int] = {}
        leaks = []
        for b in biens:
            by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
            cp = b.get("code_postal") or ""
            if cp and cp[:2] != b["departement"]:
                leaks.append((b["departement"], cp, b["url"]))
            if b["departement"] not in depts:
                leaks.append(("HORS-CIBLE", b["departement"], b["url"]))
        print("Par département :", dict(sorted(by_dept.items())))
        print("FUITES (cp[:2] != dept ou hors-cible) :", len(leaks))
        for lk in leaks[:10]:
            print("  LEAK", lk)
        for b in biens[:10]:
            print(
                f"  [{b['departement']}|{b.get('code_postal') or '?????'}]"
                f" {b['titre'][:50]} — {b['prix']}€ — {b.get('surface') or '?'}m²"
                f" — {b.get('pieces') or '?'}p — {b['ville']}"
            )

    asyncio.run(_test())

"""scrapers/notaires_yonne.py — Chambre des notaires de l'Yonne (89)

Méthode : scrape_simple (httpx) — SSR HTML (portail notarial départemental,
technologie Prisme/Novius, même gabarit que notaires_valdeloire). Couvre
nativement l'Yonne (89), département de la zone cible.

URL pattern : /petites-annonces?typeTransactions[]=VENTE&typeBiens[]=MAI&typeBiens[]=AGR&page=N
  - SSR pur (httpx suffit, pas de Playwright) ; 12 cartes / page, pagination ?page=N.
  - Filtre TYPE côté serveur via typeBiens[] (MAI=maison, AGR=propriété agricole)
    et typeTransactions[]=VENTE (exclut location / viager / enchères).

Filtre DÉPARTEMENT : pas de paramètre département dans le formulaire. On scrape
  l'inventaire complet et on POST-FILTRE strictement par département extrait du slug
  d'URL (…/{ville}-{NN}/{id}) recoupé avec le code postal de la description. 0 fuite.

Cartes : a.offer-card[href -> www.immobilier.notaires.fr/fr/annonce-immo/vente/{type}/{ville}-{NN}/{id}]
  - Prix  : <p class="notaires-color2-color"> "126000 €" (prix FAI)
  - Loc   : <p> "Brienon-sur-Armançon - Yonne (89)"
  - Surf  : <p> "113m2 - N pièces" ; Desc : <p class="text-2xs"> + "(89210)"
  - Photo : img.image[src]

Couverture (juin 2026) : ~110 maisons/propriétés VENTE dans l'Yonne (89) ; rares
  fuites limitrophes (52/58/66/77/45) filtrées (seul 45 cible serait conservé).
  0 fuite hors-zone vérifié. Complémentaire de l'API immobilier_notaires.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://chambre-yonne-89.notaires.fr"
LISTING_PATH = "/petites-annonces"
PER_PAGE = 12
MAX_PAGES = 20  # garde-fou : ~10 pages observées juin 2026

# Départements où ce portail a du stock (observé juin 2026). Cœur : 89.
COVERED_DEPTS = {"89", "45", "58"}

TYPE_BIENS = ["MAI", "AGR"]


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

    targets = departements & COVERED_DEPTS
    if not targets:
        print(
            f"[NotairesYonne] aucun dept cible dans la zone couverte "
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
                print(f"[NotairesYonne] ERR page {page}: {e}")
                break
            if r.status_code != 200:
                break

            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select("a.offer-card")
            if not cards:
                break

            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # POST-FILTRE département STRICT : 0 fuite garantie.
                if bien["departement"] not in departements:
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

            await asyncio.sleep(0.4)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print(f"[NotairesYonne] total: {len(results)} biens — par dept: {by_dept}")
    return results


def _parse_card(card) -> dict | None:
    href = card.get("href", "")
    if not href or "annonce-immo" not in href:
        return None
    url = href if href.startswith("http") else "https://www.immobilier.notaires.fr" + href

    m_slug = re.search(r"/([a-z0-9\-]+)-(\d{2,3})/(\d+)\s*$", href)
    dept = ""
    id_annonce = ""
    if m_slug:
        dept = m_slug.group(2)[:2]
        id_annonce = m_slug.group(3)
    else:
        m_id = re.search(r"/(\d+)\s*$", href)
        id_annonce = m_id.group(1) if m_id else url

    type_bien = ""
    for p in card.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if re.match(r"^\s*Vente\b", txt) and "-" in txt:
            type_bien = re.sub(r"^\s*Vente\s*-\s*", "", txt).strip()
            break
    if not type_bien:
        seg = href.split("/annonce-immo/vente/")
        if len(seg) > 1:
            type_bien = seg[1].split("/")[0].replace("-", " ")
    if _EXCLUDE_TYPE.search(type_bien) and not _KEEP_TYPE.search(type_bien):
        return None
    if type_bien and not _KEEP_TYPE.search(type_bien):
        return None
    type_bien = type_bien.strip() or "maison"

    # Localisation : "Brienon-sur-Armançon - Yonne (89)" → ville greedy.
    ville = ""
    loc_dept = ""
    for p in card.find_all("p"):
        txt = p.get_text(" ", strip=True)
        m = re.match(r"^(.+)\s-\s[A-Za-zÀ-ÿ'’\.\- ]+\((\d{2,3})\)\s*$", txt)
        if m and "Vente" not in txt:
            ville = m.group(1).strip()
            loc_dept = m.group(2)[:2]
            break
    if not dept and loc_dept:
        dept = loc_dept

    full_text = card.get_text(" ", strip=True)

    surface = None
    pieces = None
    m_surf = re.search(r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m2\b", full_text)
    if m_surf:
        try:
            surface = float(re.sub(r"[\s\xa0]", "", m_surf.group(1)).replace(",", "."))
        except ValueError:
            surface = None
    m_pc = re.search(r"(\d+)\s*pi[eè]ces?", full_text)
    if m_pc:
        pieces = int(m_pc.group(1))

    code_postal = ""
    m_cp = re.search(r"\((\d{5})\)", full_text)
    if m_cp:
        code_postal = m_cp.group(1)
        if not dept:
            dept = code_postal[:2]

    if not dept:
        return None
    if code_postal and code_postal[:2] != dept:
        dept = code_postal[:2]

    prix = None
    price_p = card.select_one("p.notaires-color2-color")
    if price_p:
        prix = _parse_price(price_p.get_text(" ", strip=True))
    if prix is None:
        m_pr = re.search(r"([\d][\d\s\xa0]{2,})\s*€", full_text)
        if m_pr:
            prix = _parse_price(m_pr.group(0))

    desc_p = card.select_one("p.text-2xs")
    description = ""
    if desc_p:
        description = desc_p.get_text(" ", strip=True)
    description = re.sub(r"&lt;br&gt;|<br\s*/?>", " ", description)

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
        "source": "notaires_yonne",
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
        "agence": "Notaires de l'Yonne",
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
            {"departements": depts, "prix_max": 0, "prix_min": 0, "surface_min": 0}
        )
        print(f"\nTotal Notaires Yonne: {len(biens)} biens")
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

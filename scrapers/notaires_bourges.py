"""scrapers/notaires_bourges.py — Conseil régional des notaires de Bourges
(Chambre interdép. Cher / Indre / Nièvre)

Méthode : scrape_simple (httpx) — SSR HTML (portail notarial régional, technologie
Novius, identique à notaires_valdeloire / notaires_yonne).
URL pattern : /petites-annonces?typeTransactions[]=VENTE&typeBiens[]=MAI&typeBiens[]=AGR&page=N
  - SSR pur (httpx suffit, pas de Playwright) ; 12 cartes / page, pagination ?page=N.
  - Filtre TYPE de bien côté serveur via typeBiens[] (MAI=maison, AGR=propriété
    agricole) et typeTransactions[]=VENTE (exclut location / viager / enchères).

Filtre DÉPARTEMENT : le portail couvre nativement le Cher (18), l'Indre (36) et la
  Nièvre (58) — cœur de la zone cible — avec quelques fuites limitrophes rares
  (44, 85, 45, 41…). Il n'expose PAS de paramètre département. On scrape l'inventaire
  complet (MAI + AGR) et on POST-FILTRE STRICT par département à partir du slug d'URL
  de chaque fiche (…/{ville}-{NN}/{id}) et du code postal extrait de la description.
  → 0 fuite hors-département garantie (contrôle code_postal[:2] / slug NN).

Cartes : a.offer-card[href -> www.immobilier.notaires.fr/fr/annonce-immo/vente/{type}/{ville}-{NN}/{id}]
  - Date      : 1er <p class="text-gray-600">  (JJ/MM/AAAA)
  - Prix FAI  : <p ...notaires-color2-color> "168000 €"  (charge acquéreur, frais inclus)
  - Type      : <p> "Vente - Maison / villa"
  - Loc       : <p> "Bourges - Cher (18)"
  - Surf/pcs  : <p> "112m2 - 5 pièces"
  - Desc      : dernier <p class="text-2xs"> "Commune de BOURGES (18000)<br>…"  → CP exact
  - Photo     : img.image[src]

Couverture (juin 2026) : ~50 maisons 18, ~40 dans 36, ~40 dans 58 — bon stock natif
sur la zone cible. Complémentaire de notaires_valdeloire (45/41/37) et notaires_yonne
(89/45/58) qui couvrent d'autres chambres.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://conseilregional-bourges.notaires.fr"
LISTING_PATH = "/petites-annonces"
MAX_PAGES = 30  # garde-fou : ~12 pages observées (18/36/58)

# Départements où ce portail a effectivement du stock (observé juin 2026).
# Cœur : 18 / 36 / 58 (Cher / Indre / Nièvre). Présence marginale : 45 / 41.
COVERED_DEPTS = {"18", "36", "58", "45", "41", "37", "89"}

# Types de bien (typeBiens[]) demandés côté serveur : maisons + propriétés agricoles.
TYPE_BIENS = ["MAI", "AGR"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# On ne retient que maisons / propriétés ; rejet explicite des types non-bâtis/divers.
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
            f"[NotairesBourges] aucun dept cible dans la zone couverte "
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
                print(f"[NotairesBourges] ERR page {page}: {e}")
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
    print(f"[NotairesBourges] total: {len(results)} biens — par dept: {by_dept}")
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
        seg = href.split("/annonce-immo/vente/")
        if len(seg) > 1:
            type_bien = seg[1].split("/")[0].replace("-", " ")
    if _EXCLUDE_TYPE.search(type_bien) and not _KEEP_TYPE.search(type_bien):
        return None
    if type_bien and not _KEEP_TYPE.search(type_bien):
        return None
    type_bien = type_bien.strip() or "maison"

    # Localisation : <p> "Bourges - Cher (18)"
    ville = ""
    loc_dept = ""
    for p in card.find_all("p"):
        txt = p.get_text(" ", strip=True)
        m = re.match(r"^(.+?)\s+-\s+[\w'\-éèêàâ ]+\((\d{2,3})\)\s*$", txt)
        if m and "Maison" not in txt and "Vente" not in txt:
            ville = m.group(1).strip()
            loc_dept = m.group(2)[:2]
            break
    if not dept and loc_dept:
        dept = loc_dept

    # Surface + pièces : <p> "112m2 - 5 pièces"
    surface = None
    pieces = None
    full_text = card.get_text(" ", strip=True)
    m_surf = re.search(r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m2\b", full_text)
    if m_surf:
        try:
            surface = float(re.sub(r"[\s\xa0]", "", m_surf.group(1)).replace(",", "."))
        except ValueError:
            surface = None
    m_pc = re.search(r"(\d+)\s*pi[eè]ces?", full_text)
    if m_pc:
        pieces = int(m_pc.group(1))

    # Code postal exact depuis la description : "Commune de BOURGES (18000)"
    code_postal = ""
    m_cp = re.search(r"\((\d{5})\)", full_text)
    if m_cp:
        code_postal = m_cp.group(1)
        if not dept:
            dept = code_postal[:2]

    if not dept:
        return None

    # Prix : 1er <p notaires-color2-color> "168000 €" (FAI / charge acquéreur)
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
        description = desc_p.get_text(" ", strip=True)
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
        "source": "notaires_bourges",
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
        "agence": "Notaires Cher-Indre-Nièvre",
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
        print(f"\nTotal Notaires Bourges: {len(biens)} biens")
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
        print("FUITES :", len(leaks))
        for lk in leaks[:10]:
            print("  LEAK", lk)
        for b in biens[:10]:
            print(
                f"  [{b['departement']}|{b.get('code_postal') or '?????'}]"
                f" {b['titre'][:48]} — {b['prix']}€ — {b.get('surface') or '?'}m²"
                f" — {b.get('pieces') or '?'}p — {b['ville']}"
            )

    asyncio.run(_test())

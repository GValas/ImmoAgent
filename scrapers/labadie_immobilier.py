"""scrapers/labadie_immobilier.py — Labadie Immobilier (4 agences indépendantes)

Méthode : scrape_simple (httpx) — SSR HTML (CMS la-boite-immo)
Réseau : 4 agences Calvados (14) / Manche (50) / Orne (61)
         → Vire, Villedieu, Passais, Saint-Jean-le-Thomas.
URL pattern : /a-vendre/{page}   (1..N, ~12 biens/page, 200 OK)
              → PAS de filtre département côté serveur. Le réseau est borné
                géographiquement à 14/50/61, donc on scrape tout le catalogue
                (≈150 biens) et on POST-FILTRE strictement CP[:2] ∈ départements.

Cartes : li.panelBien (article schema.org/Product)
  - URL   : onclick "location.href='/{id}-{slug}.html'" (ou a.btn-listing[href])
  - Prix  : span[itemprop=price][content]   (ex content="13000")
  - Photo : img[itemprop=image][src]  (//labadieimmo.staticlbi.com/...)
  - Titre : h1[itemprop=name]
  - h2    : "{Type}  {surface} m² -  {N Pièces} -  {Ville} (CODEPOSTAL)"
  - Descr : p[itemprop=description]
  - Réf   : span.ref[itemprop=productID]  ("Ref 7618AL")

Surface terrain : non structurée dans la carte → tentative depuis la description
                  (ex "terrain de 1200 m²"). None si absente.
Type de bien : déduit du 1er mot du h2 (Maison/Appartement/Terrain/Autre...).
               On ne conserve que maisons / propriétés (exclut appartement,
               terrain, local/commerce/parking).

Couverture : stock total ≈150 biens, exclusivement 14 (≈97), 50 (≈46), 61 (≈7).
             "0 fuite" hors-zone vérifié. Aucun bien hors 14/50/61.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.labadie-immobilier.fr"
MAX_PAGES = 20
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien (1er mot du h2) à conserver
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|long[èe]re|manoir|ch[âa]teau|moulin|"
    r"demeure|domaine|mas|g[îi]te|corps de ferme|maison de village|b[âa]timent",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"murs",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/a-vendre/{page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Labadie] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("li.panelBien")
            if not cards:
                break

            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # Post-filtre département STRICT (réseau borné 14/50/61 ;
                # aucune fuite hors-zone n'est tolérée).
                cp = bien["code_postal"]
                if not cp or cp[:2] not in departements:
                    continue
                bien["departement"] = cp[:2]

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

            await asyncio.sleep(0.5)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print(f"[Labadie] {len(results)} annonces retenues (par dept: {by_dept})")
    return results


def _parse_card(card) -> dict | None:
    # URL détail : a.btn-listing ou onclick
    href = ""
    a = card.select_one("a.btn-listing[href]")
    if a:
        href = a.get("href", "")
    if not href:
        onclick = card.get("onclick", "")
        m = re.search(r"location\.href=['\"]([^'\"]+)['\"]", onclick)
        if m:
            href = m.group(1)
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce : id numérique du slug, puis ref en complément
    id_num = ""
    m = re.search(r"/(\d+)-", href)
    if m:
        id_num = m.group(1)
    ref_el = card.select_one(".ref")
    ref = ""
    if ref_el:
        ref = re.sub(r"^\s*Ref\s*", "", ref_el.get_text(strip=True), flags=re.I)
    id_annonce = id_num or ref or url

    # Type / surface / pièces / ville / CP depuis le h2
    h2_el = card.select_one(".bienTitle h2")
    h2_text = h2_el.get_text(" ", strip=True) if h2_el else ""
    type_bien, surface, pieces, ville, code_postal = _parse_h2(h2_text)

    if not _KEEP_TYPE.search(type_bien) or (
        _EXCLUDE_TYPE.search(type_bien) and not _KEEP_TYPE.search(type_bien)
    ):
        return None

    # Titre
    title_el = card.select_one("h1[itemprop=name]") or card.select_one(
        ".bienTitle h1"
    )
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre or titre.strip(". ") == "":
        titre = f"{type_bien} {ville}".strip()

    # Description
    desc_el = card.select_one("p[itemprop=description]")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Prix
    prix = None
    price_el = card.select_one("[itemprop=price]")
    if price_el:
        content = price_el.get("content") or price_el.get_text(" ", strip=True)
        prix = _parse_price(content)

    # Surface terrain : non structurée → depuis la description
    surface_terrain = _parse_terrain(description) or _parse_terrain(titre)

    # Photos
    photos = []
    for img in card.select("img[itemprop=image], .panel-body img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    # dédup en conservant l'ordre
    photos = list(dict.fromkeys(photos))[:PHOTOS_PER_CARD]

    return {
        "source": "labadie_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien.lower() or "maison",
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else None,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Labadie Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_h2(text: str):
    """'Maison  127 m² -  2 Pièces -  Saint-Hilaire (50600)'
    → ('Maison', 127.0, 2, 'Saint-Hilaire', '50600')"""
    type_bien = ""
    surface = None
    pieces = None
    ville = ""
    code_postal = ""
    if not text:
        return type_bien, surface, pieces, ville, code_postal

    # Code postal + ville (dernier segment "Ville (NNNNN)").
    # La ville peut être composée/à tirets (Saint-Hilaire-Du-Harcouët) → on
    # capture tout ce qui précède la parenthèse, après le dernier séparateur
    # " - " (qui suit pièces/surface).
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        code_postal = m_cp.group(1)
        before = text[: m_cp.start()]
        # Le dernier bloc après " - " (ou après "Pièces"/"pièce") est la ville
        seg = re.split(r"\s-\s|pi[èe]ces?", before, flags=re.IGNORECASE)[-1]
        ville = seg.strip(" -\n\t")

    # Surface habitable (1er "NN m²" du h2)
    m_s = re.search(r"([\d]+(?:[.,]\d+)?)\s*m²", text)
    if m_s:
        try:
            f = float(m_s.group(1).replace(",", "."))
            if 5 <= f <= 5000:
                surface = f
        except ValueError:
            pass

    # Pièces
    m_p = re.search(r"(\d+)\s*pi[èe]ces?", text, re.IGNORECASE)
    if m_p:
        pieces = int(m_p.group(1))

    # Type = 1er mot (avant le 1er chiffre)
    m_t = re.match(r"\s*([A-Za-zÀ-ÿ' ]+?)\s*\d", text)
    if m_t:
        type_bien = m_t.group(1).strip()
    else:
        type_bien = text.split()[0] if text.split() else ""

    return type_bien, surface, pieces, ville, code_postal


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", str(text)).replace(",", ".")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_terrain(text: str) -> float | None:
    """'terrain de 1200 m²' / 'terrain de 6986 m' → 1200.0"""
    if not text:
        return None
    m = re.search(
        r"terrain[^.\d]{0,20}?([\d\s\xa0]{2,})\s*m", text, re.IGNORECASE
    )
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 10 <= f <= 5_000_000:
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
    print(f"\nTotal Labadie Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

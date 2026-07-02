"""scrapers/valdeloire_sothebys.py — Val de Loire Sologne Sotheby's International Realty

Méthode : scrape_simple (httpx, SSR). Agence de prestige (Orléans / Tours / Sologne,
Val de Loire) — châteaux, manoirs, propriétés de chasse, demeures de caractère.
Inventaire NATIONAL très restreint (~25 annonces, concentrées sur le 37 + couronne).

Pas de page de résultats paginée exploitable sans session (le formulaire de recherche
poste vers une page de résultats stateful ; les pages thématiques n'affichent qu'un
carrousel de 10). On part donc du **sitemap des biens** (`/sitemap_2_fr.xml`), qui
liste TOUTES les annonces en vente avec un slug riche :
    /ref-{ref}/vente-{type}-{ville}-{N}-pieces[-{M}-chambres]-{CP5}/
On post-filtre par `code_postal[:2]`, puis on récupère prix / surface / description
sur chaque page détail survivante via le JSON-LD `Offer`.

ATTENTION filtre département : le `postalCode` du JSON-LD `seller.address` est celui
de l'AGENCE (Tours 37000), JAMAIS celui du bien → on filtre uniquement sur le CP du
slug d'URL (= localisation réelle du bien). 0 fuite vérifiée.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.valdeloire-sologne-sothebysrealty.com"
SITEMAP_URL = f"{BASE_URL}/sitemap_2_fr.xml"
DETAIL_CONCURRENCY = 6


# Slug : /ref-or2-252/vente-maison-st-cyran-du-jambot-16-pieces-10-chambres-36700/
_SLUG_RE = re.compile(
    r"/ref-([a-z0-9]+-\d+)/"
    r"vente-([a-z]+)-"               # type
    r"(.+?)"                          # ville (slug)
    r"(?:-(\d+)-pieces?)?"            # pièces (optionnel)
    r"(?:-(\d+)-chambres?)?"          # chambres (optionnel)
    r"-(\d{5})/?$"                    # code postal
)

_TYPE_MAP = {
    "maison": "maison",
    "propriete": "propriété",
    "chateau": "château",
    "moulin": "moulin",
    "hotel": "hôtel particulier",
    "manoir": "manoir",
    "appartement": "appartement",
    "immeuble": "immeuble",
    "terrain": "terrain",
}


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=30) as client:
        # 1) sitemap → toutes les annonces
        try:
            r = await client.get(SITEMAP_URL)
            r.raise_for_status()
        except Exception as e:
            print(f"[ValdeLoireSothebys] sitemap KO : {e}")
            return []

        locs = [l for l in re.findall(r"<loc>([^<]+)</loc>", r.text) if "/ref-" in l]

        # 2) parse slug + post-filtre département (CP du slug = localisation du bien)
        candidates = []
        for url in locs:
            base = _parse_slug(url)
            if not base:
                continue
            dept = base["code_postal"][:2]
            if departements and dept not in departements:
                continue
            base["departement"] = dept
            candidates.append(base)

        # 3) enrichissement détail (prix, surface, description) en parallèle
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def enrich(bien: dict) -> dict:
            async with sem:
                try:
                    rd = await client.get(bien["url"])
                    if rd.status_code == 200:
                        _fill_detail(bien, rd.text)
                except Exception as e:
                    print(f"[ValdeLoireSothebys] détail KO {bien['url'][-30:]} : {e}")
            return bien

        results = await asyncio.gather(*(enrich(b) for b in candidates))

    results = list(results)
    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[ValdeLoireSothebys] Dept {dept}: {n} annonce(s)")
    print(f"[ValdeLoireSothebys] {len(results)} annonce(s) dans les départements ciblés")
    return results


def _parse_slug(url: str) -> dict | None:
    m = _SLUG_RE.search(url)
    if not m:
        return None
    ref, typ, ville_slug, pieces, chambres, cp = m.groups()
    type_bien = _TYPE_MAP.get(typ, typ)
    ville = ville_slug.replace("-", " ").title()
    return {
        "source": "valdeloire_sothebys",
        "url": url,
        "id_annonce": ref,
        "titre": "",                  # rempli au détail
        "type_bien": type_bien,
        "description": None,
        "departement": cp[:2],
        "ville": ville,
        "code_postal": cp,
        "surface": None,
        "surface_terrain": None,
        "pieces": int(pieces) if pieces else None,
        "chambres": int(chambres) if chambres else None,
        "prix": None,
        "dpe": None,
        "photos": [],
        "agence": "Val de Loire Sologne Sotheby's",
    }


def _fill_detail(bien: dict, html: str) -> None:
    soup = BeautifulSoup(html, "html.parser")

    # JSON-LD Offer : source autoritaire pour prix / catégorie / description / image.
    offer = None
    for sc in soup.select('script[type="application/ld+json"]'):
        raw = (sc.string or sc.get_text() or "").replace("&nbsp;", " ").replace("&euro;", "EUR")
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, dict) and data.get("@type") == "Offer":
            offer = data
            break

    if offer:
        prix = _parse_num(str(offer.get("price", "")))
        if prix:
            bien["prix"] = prix
        cat = offer.get("category")
        if cat:
            bien["type_bien"] = _TYPE_MAP.get(str(cat).lower(), bien["type_bien"])
        desc = offer.get("description")
        if desc:
            bien["description"] = desc.strip()[:500]
        img = offer.get("image")
        if img and isinstance(img, str) and img.startswith("http"):
            bien["photos"] = [img]
        name = offer.get("name") or ""
        if name:
            bien["titre"] = re.sub(r"\s+", " ", name).strip()

    # Titre depuis h1 si pas dans le JSON-LD
    if not bien["titre"] and soup.h1:
        bien["titre"] = re.sub(r"\s+", " ", soup.h1.get_text(" ", strip=True)).strip()

    # Surface habitable : depuis le titre/name ("… 640 m²") puis fallback texte.
    src_txt = (bien["titre"] or "").replace("\xa0", " ")
    m_surf = re.search(r"(\d[\d\s]*)\s*m²", src_txt)
    if m_surf:
        bien["surface"] = _parse_num(m_surf.group(1))

    # Ville propre depuis le titre ("Vente {Type} {Ville} {N} Pièces …") — le slug
    # colle parfois des mots de sous-type dans la ville (ex. "propriété de chasse").
    # Ville (avec accents) depuis le titre : segment entre le type et "{N} Pièces".
    # Le titre commence par "Vente {Type…} {Ville} {N} Pièces …" ; le type peut être
    # multi-mots (Hôtel Particulier, Propriété de Chasse) → on retire le 1er mot
    # capitalisé restant s'il correspond à un libellé de type connu.
    m_ville = re.search(r"^Vente\s+(.+?)\s+\d+\s*Pièces?", src_txt, re.IGNORECASE)
    if m_ville:
        words = m_ville.group(1).split()
        # retire les mots de tête appartenant au libellé du type
        type_words = set(re.split(r"[\s'’-]+", bien["type_bien"].lower()))
        while words and re.split(r"[\s'’-]+", words[0].lower())[0] in type_words:
            words.pop(0)
        cand = " ".join(words).strip()
        if cand and not re.match(r"^\d", cand):
            bien["ville"] = cand

    # Surface terrain (si mentionnée dans la description)
    if bien.get("description"):
        m_ter = re.search(
            r"terrain[^\d]{0,30}?([\d\s]+)\s*m²", bien["description"], re.IGNORECASE
        )
        if m_ter:
            bien["surface_terrain"] = _parse_num(m_ter.group(1))


def _parse_num(text: str) -> float | None:
    cleaned = re.sub(r"[^\d.]", "", text.replace("\xa0", "").replace(" ", "").replace(",", "."))
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    depts = criteres.departements
    biens = asyncio.run(search({"departements": depts}))

    print(f"\nDépartements ciblés : {depts}")
    print(f"Total Val de Loire Sotheby's : {len(biens)} annonce(s)")
    leaks = [b for b in biens if b["code_postal"][:2] not in {str(d).zfill(2) for d in depts}]
    print(f"FUITES hors-département : {len(leaks)}")
    for b in biens[:15]:
        print(
            f"  [{b['code_postal']}] {b['type_bien']:<12} {b['ville']:<22} "
            f"{b['prix'] or '?'}€ — {b.get('surface') or '?'}m² — {b['pieces']}p "
            f"— {b['url'][-32:]}"
        )

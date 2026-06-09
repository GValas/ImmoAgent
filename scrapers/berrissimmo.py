"""scrapers/berrissimmo.py — Berrissimmo (agence locale Berry / Sologne)

Méthode : scrape_simple (httpx) — SSR Apache, HTML brut (200, CP 36 dans le HTML).
URL pattern : /acheter                         (listing complet, page unique)
              /achat,{type}-{slug},E{id}        (page détail)

Filtre département : agence mono-département de fait (La Châtre, 36400).
  Tout le catalogue est en Indre (36) ; il n'existe pas de paramètre de
  département (inutile). On applique malgré tout un post-filtre STRICT
  code_postal[:2] == dept de sécurité → 0 fuite hors-zone.
  Conséquence : si 36 n'est pas dans les départements cibles, search() renvoie [].

Cartes : div.tuile-container
  - Lien détail : a[href*=achat]  (ex: a.firstLoad[data-img] quand présent)
  - Titre/ville : div.titre        (ville en capitales, ex "LA CHATRE")
  - Réf         : span.ref         ("Ref: E1020")
  - Prix        : div.prix         ("139.100€")
  - Desc + CP   : div.desc         ("… – La Châtre (36400) …")
  - KPI         : div.kpi > div.item → "3 chambre(s)", "434m² de terrain",
                  "7 pièce(s)", "160m² de surface", "1 salle(s) d'eau"…
  - Photo       : a.firstLoad[data-img]  (https://p-static.alveen.com/photos/…)

CP manquant sur quelques cartes → repli sur le <title> de la page détail
(« … VILLE (36xxx) … »), seul l'Indre étant concerné.

Type de bien : déduit du segment d'URL /achat,{type}-… (maison/terrain/immeuble…).
On ne garde que maisons / propriétés (terrains et locaux exclus).

Volume : niche (~8-9 annonces, page unique).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.berrissimmo.fr"
LISTING_URL = f"{BASE_URL}/acheter"
PHOTOS_PER_CARD = 1  # 1 vignette par carte sur le listing ; détail non scrapé en masse

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types (segment d'URL) à conserver vs exclure
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|longere|manoir|"
    r"château|chateau|moulin|demeure|domaine|mas|gite|gîte|fermette|immeuble|"
    r"maison-de-village|corps-de-ferme|grange",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"terrain|local|commerce|garage|parking|bureau|fonds|appartement",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Agence mono-département (36). Si 36 hors cible → rien à scraper.
    if "36" not in departements:
        print("[Berrissimmo] Dept 36 hors zone cible → 0 annonce (mono-36)")
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        try:
            r = await client.get(LISTING_URL)
        except Exception as e:
            print(f"[Berrissimmo] Erreur requête listing : {e}")
            return []
        if r.status_code != 200:
            print(f"[Berrissimmo] Listing status {r.status_code}")
            return []

        cards = BeautifulSoup(r.text, "html.parser").select("div.tuile-container")
        print(f"[Berrissimmo] {len(cards)} cartes sur le listing")

        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception as e:
                print(f"[Berrissimmo] Erreur parse carte : {e}")
                continue
            if not bien:
                continue

            # CP manquant → repli sur la page détail
            if not bien["code_postal"] and bien["url"]:
                cp = await _cp_from_detail(client, bien["url"])
                if cp:
                    bien["code_postal"] = cp
                await asyncio.sleep(0.5)

            # Post-filtre dept STRICT : 0 fuite
            cp = bien["code_postal"]
            if not cp or cp[:2] != "36":
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

    print(f"[Berrissimmo] Dept 36 : {len(results)} annonces retenues")
    return results


async def _cp_from_detail(client: httpx.AsyncClient, url: str) -> str | None:
    """Récupère le code postal depuis le <title> de la page détail
    (« … VILLE (36xxx) … »). Ne s'utilise que si la carte n'a pas de CP."""
    try:
        r = await client.get(url)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    m = re.search(r"\((36\d{3})\)", title)
    if m:
        return m.group(1)
    # repli : premier CP 36xxx du titre / h1
    h1 = soup.select_one("h1")
    h1txt = h1.get_text(" ", strip=True) if h1 else ""
    m = re.search(r"\b(36\d{3})\b", title + " " + h1txt)
    return m.group(1) if m else None


def _parse_card(card) -> dict | None:
    link = card.select_one("a.firstLoad") or card.select_one("a[href*=achat]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien depuis le segment d'URL : /achat,{type}-{slug},E{id}
    seg = href.split("achat,")[-1]
    type_seg = seg.split("-")[0] if "-" in seg else seg.split(",")[0]
    type_seg = type_seg.strip().lower()
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg or "maison"

    # Référence / id
    ref_el = card.select_one("span.ref")
    ref = ref_el.get_text(strip=True).replace("Ref:", "").strip() if ref_el else ""
    if not ref:
        m = re.search(r",E(\w+)$", href)
        ref = ("E" + m.group(1)) if m else url
    id_annonce = ref

    # Ville (div.titre, en capitales)
    titre_el = card.select_one("div.titre")
    ville_raw = titre_el.get_text(" ", strip=True) if titre_el else ""
    ville = _titlecase_ville(ville_raw)

    # Description + code postal
    desc_el = card.select_one("div.desc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""
    code_postal = ""
    m_cp = re.search(r"\((\d{5})\)", description)
    if not m_cp:
        m_cp = re.search(r"\b(\d{5})\b", card.get_text(" ", strip=True))
    if m_cp:
        code_postal = m_cp.group(1)

    # Titre : 1ʳᵉ ligne de la description (sans emoji) ou type + ville
    titre = re.sub(r"^[^\w]+", "", description.split("\n")[0]).strip()
    titre = re.split(r"\s+–\s+|\s+-\s+", titre)[0].strip()
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Prix
    prix_el = card.select_one("div.prix")
    prix = _parse_price(prix_el.get_text(" ", strip=True) if prix_el else "")

    # KPI : pièces / chambres / surfaces / terrain
    kpi_text = ""
    kpi_el = card.select_one("div.kpi")
    if kpi_el:
        kpi_text = kpi_el.get_text(" ", strip=True)
    pieces = _parse_int(r"(\d+)\s*pi[eè]ce", kpi_text)
    chambres = _parse_int(r"(\d+)\s*chambre", kpi_text)
    surface = _parse_m2(r"(\d[\d\s\xa0.,]*)\s*m²\s*de\s*surface", kpi_text)
    surface_terrain = _parse_m2(r"(\d[\d\s\xa0.,]*)\s*m²\s*de\s*terrain", kpi_text)

    # Photo (vignette)
    photos = []
    if link is not None:
        img = link.get("data-img") or ""
        if img.startswith("http"):
            photos.append(img)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "berrissimmo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": "36",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Berrissimmo",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _titlecase_ville(text: str) -> str:
    """'LA CHATRE' → 'La Chatre' ; conserve les tirets."""
    text = text.strip()
    if not text:
        return ""
    if text.isupper():
        return " ".join(
            "-".join(p.capitalize() for p in word.split("-")) for word in text.split()
        )
    return text


def _parse_price(text: str) -> float | None:
    """'139.100€' → 139100.0 (le point est séparateur de milliers ici)."""
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = cleaned.replace(".", "").replace(",", "")
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_m2(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
    # un éventuel point de milliers dans une surface est improbable < 10000
    try:
        f = float(val)
        return f if 1 <= f <= 100000 else None
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
    print(f"\nTotal Berrissimmo: {len(biens)} annonces")
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

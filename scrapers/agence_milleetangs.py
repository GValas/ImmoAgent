"""scrapers/agence_milleetangs.py — Agence des Mille et Étangs (Le Blanc, Brenne)

Agence locale du Parc naturel régional de la Brenne (Indre 36 : Le Blanc,
Mézières-en-Brenne, Saint-Gaultier, Bélâbre…). Biens de campagne, longères,
fermes, maisons de bourg. Bon vivier de propriétés rurales.

Méthode : scrape_simple (httpx) — vieux site PHP, liste en TABLEAU SSR.
URL pattern :
  - Liste : /location-vente/vente.php?num_page=N   (≈18 pages, 5 biens/page)
  - Détail : /location-vente/fiche-vente.php?idAnnonce={id}
  - Photo  : /inclusions/getimage.php?ID={photoId}&table=photos

Filtre département : agence mono-zone Brenne/Indre, PAS de filtre serveur par
  dept → POST-FILTRE strict sur code_postal[:2] ∈ départements cibles. 0 fuite.

Cartes : tr[valign="top"]  → 3 colonnes (Photo | Description | Prix)
  - Desc (td col2) : "Maison - Ref. 5790 ... 36300 LE BLANC Indre ... 136 m² ..."
                     → type + référence + CODE POSTAL + ville + surface (1er m²)
  - Prix (td col3) : "117 150,00 €"
  - Photo          : a.img > img[src=getimage.php?...]
Détail (fiche-vente) : description complète + galerie getimage.php + chambres.

Types : on garde maison / propriété / longère / ferme / manoir / moulin /
  domaine / demeure ; on exclut appartement / terrain / immeuble / commerce.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "http://www.agencedesmilleetangs.com"
LIST_URL = f"{BASE_URL}/location-vente/vente.php"
DETAIL_BASE = f"{BASE_URL}/location-vente/"
MAX_PAGES = 20
PHOTOS_PER_BIEN = 10
CONCURRENCY = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|fermette|longere|longère|manoir|"
    r"chateau|château|moulin|demeure|domaine|gite|gîte|corps de ferme|pavillon|"
    r"grange",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|immeuble|local|commerce|garage|parking|bureau|fonds|"
    r"étang|etang",
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
        parsed: dict[str, dict] = {}
        for page in range(1, MAX_PAGES + 1):
            try:
                r = await client.get(LIST_URL, params={"num_page": page})
                if r.status_code != 200:
                    break
            except Exception as e:
                print(f"[MilleEtangs] Erreur page {page}: {e}")
                break

            rows = BeautifulSoup(r.text, "html.parser").select('tr[valign="top"]')
            new_on_page = 0
            for row in rows:
                try:
                    bien = _parse_row(row)
                except Exception:
                    continue
                if bien and bien["id_annonce"] not in parsed:
                    parsed[bien["id_annonce"]] = bien
                    new_on_page += 1
            if new_on_page == 0:
                break
            await asyncio.sleep(0.5)

        print(f"[MilleEtangs] {len(parsed)} annonces listées")

        retained = []
        for bien in parsed.values():
            cp = bien.get("code_postal") or ""
            if not cp or cp[:2] not in departements:
                continue
            tb = bien.get("type_bien") or ""
            if _EXCLUDE_TYPE.search(tb) and not _KEEP_TYPE.search(tb):
                continue
            if not _KEEP_TYPE.search(tb):
                continue
            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue
            retained.append(bien)

        print(f"[MilleEtangs] {len(retained)} retenues (zone + type + bornes) "
              f"→ enrichissement détail")

        sem = asyncio.Semaphore(CONCURRENCY)

        async def _enrich(bien: dict) -> dict:
            async with sem:
                try:
                    await _fill_detail(client, bien)
                except Exception as e:
                    print(f"[MilleEtangs] détail {bien['id_annonce']}: {e}")
                await asyncio.sleep(0.4)
                return bien

        retained = await asyncio.gather(*[_enrich(b) for b in retained])

    for bien in retained:
        cp = bien.get("code_postal") or ""
        if cp and cp[:2] in departements:
            results.append(bien)

    print(f"[MilleEtangs] {len(results)} biens retenus")
    return results


def _parse_row(row) -> dict | None:
    tds = row.select("td")
    if len(tds) < 3:
        return None

    link = row.select_one('a[href*="fiche-vente.php"]')
    if not link:
        return None
    href = link.get("href", "")
    m_id = re.search(r"idAnnonce=(\d+)", href)
    if not m_id:
        return None
    id_annonce = m_id.group(1)
    url = f"{DETAIL_BASE}fiche-vente.php?idAnnonce={id_annonce}"

    desc_full = tds[1].get_text(" ", strip=True)
    # type = 1er mot avant " - "
    m_type = re.match(r"\s*([A-Za-zÀ-ÿ'/ ]+?)\s*-\s*Ref", desc_full)
    type_bien = (m_type.group(1).strip().lower() if m_type else "maison")

    ref = ""
    m_ref = re.search(r"Ref\.\s*(\S+)", desc_full)
    if m_ref:
        ref = m_ref.group(1)

    code_postal = ""
    ville = ""
    # CP suivi de la ville en MAJUSCULES (tokens tout-capitales consécutifs)
    # tokens ville = mots TOUT-MAJUSCULES de ≥2 lettres (évite d'attraper
    # l'initiale d'un mot capitalisé suivant comme "Maison"/"Indre")
    m_loc = re.search(
        r"\b(\d{5})\s+((?:[A-ZÀ-Ÿ]{2,}[A-ZÀ-Ÿ'\-]*\s+)*[A-ZÀ-Ÿ]{2,}[A-ZÀ-Ÿ'\-]*)",
        desc_full,
    )
    if m_loc:
        code_postal = m_loc.group(1)
        ville = m_loc.group(2).strip()
        ville = re.sub(r"\s+INDRE$", "", ville, flags=re.IGNORECASE).strip().title()
    else:
        m_cp = re.search(r"\b(\d{5})\b", desc_full)
        if m_cp:
            code_postal = m_cp.group(1)

    surface = _first_int(r"(\d[\d\s\xa0]*)\s*m[²2]", desc_full)
    surface_terrain = _parse_terrain(desc_full)

    prix = _parse_price(tds[2].get_text(" ", strip=True))

    photos = []
    img = row.select_one("img")
    if img and img.get("src"):
        src = img.get("src")
        src = src.replace("..", "").lstrip("/")
        if "getimage" in src:
            photos.append(f"{BASE_URL}/{src}")

    return {
        "source": "agence_milleetangs",
        "url": url,
        "id_annonce": id_annonce,
        "titre": f"{type_bien.title()} {ville}".strip()[:150],
        "type_bien": type_bien,
        "description": desc_full[:1200],
        "departement": code_postal[:2] if code_postal else None,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": _first_int(r"(\d+)\s*chambre", desc_full),
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Agence des Mille et Étangs",
    }


async def _fill_detail(client: httpx.AsyncClient, bien: dict) -> None:
    r = await client.get(bien["url"])
    if r.status_code != 200:
        return
    t = r.text

    photos = []
    for ph in re.findall(r"getimage\.php\?ID=\d+&table=photos[^\"' ]*", t):
        full = f"{BASE_URL}/inclusions/{ph}"
        if full not in photos:
            photos.append(full)
    if photos:
        bien["photos"] = photos[:PHOTOS_PER_BIEN]

    if bien.get("chambres") is None:
        bien["chambres"] = _first_int(r"(\d+)\s*chambre", t)
    if bien.get("surface_terrain") is None:
        bien["surface_terrain"] = _parse_terrain(t)
    if bien.get("surface") is None:
        s = _first_int(r"(\d[\d\s\xa0]*)\s*m[²2]\s*habitable", t) or \
            _first_int(r"habitable[^0-9]{0,15}(\d[\d\s\xa0]*)\s*m[²2]", t)
        if s and 8 <= s <= 2000:
            bien["surface"] = s


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    # "117 150,00 €" → 117150
    m = re.search(r"([\d\s\xa0]+)(?:,\d+)?\s*€", text)
    if not m:
        return None
    cleaned = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    if v is not None and v < 1000:
        return None
    return v


def _parse_terrain(text: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*ha\b", text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(",", ".")) * 10000
        except ValueError:
            pass
    m = re.search(
        r"(?:terrain|jardin|parc)[^0-9]{0,25}(\d[\d\s\xa0]{2,6})\s*m[²2]",
        text,
        re.IGNORECASE,
    )
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            return float(val)
        except ValueError:
            pass
    return None


def _first_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        return int(val)
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
    print(f"\nTotal Agence des Mille et Étangs: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

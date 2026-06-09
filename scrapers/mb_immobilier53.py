"""scrapers/mb_immobilier53.py — MB Immobilier (Château-Gontier, Mayenne)

Agence locale de Château-Gontier-sur-Mayenne (Mayenne 53, déborde sur le Nord
Maine-et-Loire 49). Maisons, demeures, propriétés.

Méthode : scrape_simple (httpx) — SSR HTML (CMS Hektor récent).
URL pattern :
  - Liste  : /annonces/transaction/Vente.html   (catalogue, cartes .listing-item)
  - Détail : /fiches/{office-ids}_{id}/{slug}.html
             → table de caractéristiques complète (li > 2× div.col-sm-6
               "Libellé / Valeur") : Type de bien, Code postal, Ville, Prix,
               Surface, Surface terrain, Nombre pièces, Chambres, DPE…

Filtre département : agence mono-zone, PAS de filtre serveur → POST-FILTRE strict
  sur code_postal[:2] ∈ départements cibles (CP lu dans la fiche détail). 0 fuite.

Cartes liste : a[href*=/fiches/] → .product-name (type + ville en MAJ), prix,
  pièces / surface (.data-list__item--value), réf, photos
  (catalog/images/pr_p/...jpg). On enrichit ENSUITE via la fiche détail pour le
  CODE POSTAL fiable + surface terrain + DPE + description.

Types : on garde maison / propriété / longère / ferme / manoir / château /
  moulin / domaine / demeure ; on exclut appartement / terrain / immeuble /
  entreprise / commerce / garage.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.mb-immobilier53.com"
LIST_URL = f"{BASE_URL}/annonces/transaction/Vente.html"
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
    r"chateau|château|moulin|demeure|domaine|gite|gîte|pavillon|corps de ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|immeuble|local|commerce|garage|parking|bureau|"
    r"fonds|entreprise|entrepot|entrepôt",
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
            r = await client.get(LIST_URL)
            r.raise_for_status()
        except Exception as e:
            print(f"[MB53] Erreur liste: {e}")
            return results

        urls = _list_detail_urls(r.text)
        print(f"[MB53] {len(urls)} fiches en vente")

        sem = asyncio.Semaphore(CONCURRENCY)

        async def _fetch(url: str) -> dict | None:
            async with sem:
                try:
                    bien = await _parse_detail(client, url)
                except Exception as e:
                    print(f"[MB53] fiche {url}: {e}")
                    return None
                await asyncio.sleep(0.4)
                return bien

        biens = await asyncio.gather(*[_fetch(u) for u in urls])

    for bien in biens:
        if not bien:
            continue
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
        results.append(bien)

    print(f"[MB53] {len(results)} biens retenus (zone + type + bornes)")
    return results


def _list_detail_urls(html_text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for href in re.findall(r'(\.\./fiches/[^"\']+?\.html)', html_text):
        clean = href.replace("../", "")
        full = f"{BASE_URL}/{clean.lstrip('/')}"
        if full not in seen:
            seen.add(full)
            urls.append(full)
    return urls


async def _parse_detail(client: httpx.AsyncClient, url: str) -> dict | None:
    r = await client.get(url)
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    fields: dict[str, str] = {}
    for li in soup.select("li"):
        cols = li.select("div.col-sm-6")
        if len(cols) == 2:
            lbl = cols[0].get_text(" ", strip=True)
            val = cols[1].get_text(" ", strip=True)
            if lbl and val:
                fields[lbl.lower()] = val

    type_bien = (fields.get("type de bien") or "").lower()

    # Titre (sert aussi à fiabiliser le CP du BIEN)
    h1 = soup.select_one("h1")
    h1_txt = h1.get_text(" ", strip=True) if h1 else ""

    # ⚠️ Le champ "Code postal" de la fiche = CP de l'AGENCE (53200), PAS du bien.
    # Le CP réel du bien est dans le titre / le slug d'URL : "...à Marigné (49330)".
    code_postal = ""
    m_cp = re.search(r"\((\d{5})\)", h1_txt)
    if not m_cp:
        m_cp = re.search(r"-(\d{5})\.html", url)
    if not m_cp:
        # secours : 1er CP des balises meta/title du <head>
        m_cp = re.search(r"\((\d{5})\)", soup.title.get_text() if soup.title else "")
    code_postal = m_cp.group(1) if m_cp else ""

    # ville depuis le titre ("... à Marigné (49330)") sinon champ fiche
    ville = ""
    m_v = re.search(r"\b[àa]\s+([A-ZÀ-Ÿ][\wÀ-ÿ' \-]+?)\s*\(\d{5}\)", h1_txt)
    if m_v:
        ville = m_v.group(1).strip()
    if not ville:
        ville = (_field(fields, "ville") or "").strip().title()
    prix = _parse_num(_field(fields, "prix"))
    surface = _parse_num(_field(fields, "surface"))
    surface_terrain = _parse_num(_field(fields, "surface terrain"))
    pieces = _parse_int(_field(fields, "nombre pièces") or _field(fields, "pièces"))
    chambres = _parse_int(_field(fields, "chambres"))
    dpe = _parse_dpe(_field(fields, "consommation énergie primaire"))

    # Titre
    titre = h1_txt or f"{type_bien.title()} {ville}"

    # Référence
    ref = _field(fields, "référence") or ""
    if not ref:
        m = re.search(r"R[ée]f\s*:?\s*([A-Z0-9]+)", soup.get_text(" "), re.IGNORECASE)
        ref = m.group(1) if m else url.rsplit("_", 1)[-1].split("/")[0]

    # Description
    description = ""
    for sel in [".product-description", ".description", "[class*=descript]",
                ".page_annonce p"]:
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 50:
            description = el.get_text(" ", strip=True)
            break

    # Photos
    photos = []
    for ph in re.findall(
        r'(\.\./office\d+/[^"\']+/catalog/images/pr_[a-z]+/[0-9/]+\d+[a-z]?\.jpg)',
        r.text,
    ):
        full = f"{BASE_URL}/{ph.replace('../', '').lstrip('/')}"
        if full not in photos:
            photos.append(full)
    photos = photos[:PHOTOS_PER_BIEN]

    return {
        "source": "mb_immobilier53",
        "url": url,
        "id_annonce": ref,
        "titre": titre[:150],
        "type_bien": type_bien or "maison",
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else None,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "MB Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _field(fields: dict, key: str) -> str | None:
    return fields.get(key.lower())


def _parse_num(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"([\d\s\xa0]+(?:[.,]\d+)?)", text)
    if not m:
        return None
    cleaned = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
    try:
        v = float(cleaned)
    except ValueError:
        return None
    return v


def _parse_int(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def _parse_dpe(text: str | None) -> str | None:
    if not text:
        return None
    m = re.match(r"\s*([A-G])\b", text.strip().upper())
    return m.group(1) if m else None


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
    print(f"\nTotal MB Immobilier (53): {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']} — DPE {b.get('dpe')}"
        )

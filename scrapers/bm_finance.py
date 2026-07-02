"""scrapers/bm_finance.py — BM Finance (viager & nue-propriété, national)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress/Royal theme)
URL pattern : /annonces-viageres/{viager-occupe|viager-libre|nue-propriete}.html
              → une page liste par TYPE de produit, sans filtre département serveur
              et sans pagination (inventaire national tient sur une page, ~25 biens).

Filtre département : POST-FILTRE strict côté Python. Le code postal (5 chiffres,
              avec zéro de tête) est lu dans l'attribut `img[alt]` des cartes
              ("Viager occupé PARIS - 75006 bouquet 887E - ref 2900").
              En secours, le code département (sans zéro de tête) est dans l'URL
              détail /viager/{id}/{type}-{ville}-{DD}.html → reconstitué en CP[:2].

Cartes : a[href*="/viager/"] (deux variantes de conteneur : div.annonce et
              div.annonce-vendu) :
  - URL    : a[href]
  - CP+Ville: img[alt]  ("... VILLE - CP bouquet ... - ref NNNN")
  - Type   : span.type  ("Viager occupé" / "Viager libre" / "Nue-propriété")
  - Réf    : span.ref   ("Ref 2900")
  - Ville  : span.ville
  - Prix   : span.prixB (bouquet, "1 635 000 €") — c'est le BOUQUET, pas la
              valeur vénale ; renseigné dans `prix` faute de mieux, signalé en note.
  - Photo  : img[src]

Couverture : viager de prestige très concentré (Paris 75, PACA 83/13/06, 34, 74,
              91, 92). AUCUN bien dans la zone Val-de-Loire/Ouest cible au
              dernier test → scraper fonctionnel mais 0 stock zone.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.bm-finance.fr"
PHOTOS_PER_CARD = 5

TYPE_PAGES = (
    ("viager-occupe", "viager occupé"),
    ("viager-libre", "viager libre"),
    ("nue-propriete", "nue-propriété"),
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for slug, type_label in TYPE_PAGES:
            url = f"{BASE_URL}/annonces-viageres/{slug}.html"
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    print(f"[BMFinance] {slug}: HTTP {r.status_code}")
                    continue
                biens = _parse_page(r.text, type_label)
            except Exception as e:
                print(f"[BMFinance] Erreur {slug}: {e}")
                await asyncio.sleep(0.6)
                continue

            kept = 0
            for bien in biens:
                cp = bien["code_postal"]
                dept = cp[:2] if cp else (bien.get("departement") or "")
                if dept not in departements:
                    continue  # POST-FILTRE strict — 0 fuite hors-zone
                bien["departement"] = dept

                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue

                p = bien.get("prix") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                s = bien.get("surface") or 0
                if surface_min and s and s < surface_min:
                    continue

                seen_ids.add(aid)
                results.append(bien)
                kept += 1

            print(f"[BMFinance] {slug}: {kept} annonce(s) dans la zone")
            await asyncio.sleep(0.6)

    return results


def _parse_page(html: str, type_label: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    biens: list[dict] = []
    seen_hrefs: set[str] = set()

    for a in soup.find_all("a", href=re.compile(r"/viager/")):
        href = a.get("href", "")
        if not href or href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        try:
            bien = _parse_card(a, type_label)
        except Exception:
            continue
        if bien:
            biens.append(bien)
    return biens


def _parse_card(a, type_label: str) -> dict | None:
    href = a.get("href", "")
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce + dept (sans zéro de tête) depuis l'URL : /viager/{id}/{type}-{ville}-{DD}.html
    id_url = ""
    dept_url = ""
    m = re.search(r"/viager/([^/]+)/([^/]+?)-(\d{1,3})\.html", href)
    if m:
        id_url = m.group(1)
        dept_url = m.group(3).zfill(2)

    img = a.find("img")
    alt = img.get("alt", "") if img else ""

    # CP (5 chiffres) + ville depuis l'alt : "... VILLE - 75006 bouquet ... - ref 2900"
    code_postal = ""
    m_cp = re.search(r"-\s*(\d{5})\b", alt)
    if m_cp:
        code_postal = m_cp.group(1)

    # Type de bien
    type_el = a.select_one("span.type")
    type_bien = (
        type_el.get_text(" ", strip=True) if type_el else type_label
    ).strip() or type_label

    # Référence
    ref_el = a.select_one("span.ref")
    ref = ""
    if ref_el:
        ref = re.sub(r"(?i)^ref\s*", "", ref_el.get_text(" ", strip=True)).strip()
    if not ref:
        m_ref = re.search(r"ref\s*([0-9a-z]+)", alt, re.IGNORECASE)
        ref = m_ref.group(1) if m_ref else ""
    id_annonce = ref or id_url or url

    # Ville
    ville_el = a.select_one("span.ville")
    ville = ville_el.get_text(" ", strip=True) if ville_el else ""
    if not ville:
        m_v = re.search(r"(?:occup[ée]|libre|propri[ée]t[ée])\s+(.+?)\s+-\s+\d{5}", alt, re.IGNORECASE)
        ville = m_v.group(1).strip() if m_v else ""

    # Prix (bouquet)
    price_el = a.select_one("span.prixB")
    prix = _parse_price(price_el.get_text(" ", strip=True)) if price_el else None

    # Titre
    titre = a.get("title") or f"{type_bien} {ville}".strip()

    # Photo
    photos = []
    if img:
        src = img.get("src") or ""
        if src and not src.startswith("data:") and "viager-TMP" not in src:
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "bm_finance",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": (code_postal[:2] if code_postal else dept_url),
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": None,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "BM Finance",
    }


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
    print(f"\nTotal BM Finance: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b['type_bien']} — {b['ville']}"
        )

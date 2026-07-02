"""scrapers/promptimmo_walter.py — Walter & de Maison (mandataires)

Site : https://www.walteretdemaison.com  (marque associée : Une Maison Bleue)
Méthode : scrape_simple (httpx) — SSR WordPress / thème Houzez.

URL pattern (liste) : /annonces/page/{N}/   (≈ 9 cartes par page, ~29 pages)
  → AUCUN filtre département côté serveur : le portail est national.
    Les cartes liste n'exposent PAS le code postal (<address> vide).
  → Stratégie filtre dept : on ouvre la PAGE DÉTAIL de chaque carte retenue
    pour lire le champ « Ville » (CP + commune), puis POST-FILTRE STRICT
    code_postal[:2] ∈ départements cibles. Objectif : 0 fuite hors-zone.

Cartes liste : div.item-listing-wrap (= .item-listing-wrap.card)
  - URL détail : h2.item-title a[href]  → /annonces/{slug}/
  - Prix       : li.item-price  → "755.000 €"  (point = séparateur de milliers)
  - Type       : .h-type span   → "MAISON / VILLA" / "APPARTEMENT" / ...
  - Surface    : .h-area .hz-figure  → "170.2" (m²)
  - Chambres   : .h-beds .hz-figure
  - Photos     : attribut data-images (JSON [{image,alt},...])
  - Réf interne: data-hz-id (ex "hz-1842435")

Page détail : champs <li><strong>Label</strong> <span>valeur</span></li>
  - « Ville » → "66440 TORREILLES" (CP + commune)  ← source du code postal
  - « Référence », « Nb pièces », « Surface habitable », « Chambres »
  - Description : meta[name=description] / og:description
  DPE/GES rendus en images (DPE-1.png) → non extractibles en texte ici → None
  (le worker gallery.py enrichit le DPE plus tard si possible).

On ne garde que les maisons / propriétés (exclut appartements, terrains,
locaux, parkings) comme les autres scrapers ruraux du projet.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.walteretdemaison.com"
LIST_URL = BASE_URL + "/annonces/page/{page}/"
MAX_PAGES = 30
MAX_DETAILS = 120          # garde-fou : nb max de pages détail ouvertes par run
PHOTOS_PER_CARD = 12


# Types de bien à conserver (segment .h-type ou titre) : maisons / propriétés.
_KEEP_TYPE = re.compile(
    r"maison|villa|propriete|propriété|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps de ferme|bastide|"
    r"caractere|caractère",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|loft|studio|chambre|cave|box",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_urls: set[str] = set()
    details_done = 0

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = LIST_URL.format(page=page)
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[WalterDeMaison] Erreur liste page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select(".item-listing-wrap")
            if not cards:
                break

            for card in cards:
                pre = _parse_card(card)
                if not pre:
                    continue
                if pre["url"] in seen_urls:
                    continue
                seen_urls.add(pre["url"])

                # Pré-filtre prix / surface AVANT d'ouvrir la page détail (poli)
                p = pre.get("prix") or 0
                s = pre.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                if details_done >= MAX_DETAILS:
                    print(
                        f"[WalterDeMaison] Plafond MAX_DETAILS={MAX_DETAILS} atteint, arrêt."
                    )
                    return results

                try:
                    bien = await _enrich_detail(client, pre)
                except Exception as e:
                    print(f"[WalterDeMaison] Erreur détail {pre['url']}: {e}")
                    bien = None
                details_done += 1
                await asyncio.sleep(0.5)

                if not bien:
                    continue

                # POST-FILTRE DÉPARTEMENT STRICT (0 fuite hors-zone)
                cp = bien.get("code_postal") or ""
                if not cp or cp[:2] not in departements:
                    continue
                bien["departement"] = cp[:2]

                results.append(bien)

            await asyncio.sleep(0.4)

    print(f"[WalterDeMaison] Total retenu (zone) : {len(results)}")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("h2.item-title a") or card.select_one(
        ".listing-featured-thumb"
    )
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien
    type_el = card.select_one(".h-type span")
    type_raw = type_el.get_text(" ", strip=True) if type_el else ""

    title_el = card.select_one("h2.item-title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    type_test = f"{type_raw} {titre}"
    if _EXCLUDE_TYPE.search(type_test) and not _KEEP_TYPE.search(type_test):
        return None
    if not _KEEP_TYPE.search(type_test):
        return None
    type_bien = (type_raw or "maison").split("/")[0].strip().lower() or "maison"

    # Prix : "755.000 €" → 755000
    price_el = card.select_one(".item-price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Surface habitable (m²)
    area_el = card.select_one(".h-area .hz-figure")
    surface = _parse_float(area_el.get_text(strip=True) if area_el else "")

    # Chambres
    beds_el = card.select_one(".h-beds .hz-figure")
    chambres = _parse_int_simple(beds_el.get_text(strip=True) if beds_el else "")

    # Pièces depuis le titre ("Maison 4 pièces 84 m²")
    pieces = None
    m_p = re.search(r"(\d+)\s*pi[eè]ce", titre, re.IGNORECASE)
    if m_p:
        pieces = int(m_p.group(1))

    # Photos depuis data-images (JSON)
    photos: list[str] = []
    raw_imgs = card.get("data-images", "")
    if raw_imgs:
        try:
            for it in json.loads(raw_imgs):
                img = it.get("image") if isinstance(it, dict) else None
                if img and not img.startswith("data:"):
                    photos.append(img)
        except (json.JSONDecodeError, TypeError):
            pass
    if not photos:
        thumb = card.select_one(".listing-featured-thumb img")
        if thumb and thumb.get("src"):
            photos.append(thumb["src"])
    photos = photos[:PHOTOS_PER_CARD]

    # Réf interne (data-hz-id) en secours d'id
    hz = card.get("data-hz-id") or ""
    id_annonce = hz.replace("hz-", "") if hz else url

    return {
        "source": "promptimmo_walter",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": "",
        "ville": "",
        "code_postal": "",
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Walter & de Maison",
    }


async def _enrich_detail(client: httpx.AsyncClient, bien: dict) -> dict | None:
    """Ouvre la page détail pour lire le CP/commune (champ 'Ville') et compléter."""
    r = await client.get(bien["url"])
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    # Champs <li><strong>Label</strong> <span>valeur</span></li>
    fields = _extract_fields(soup)

    ville_raw = fields.get("ville", "")
    cp, ville = _parse_ville(ville_raw)
    if not cp:
        return None
    bien["code_postal"] = cp
    bien["ville"] = ville

    # Compléments si manquants en liste
    if bien.get("pieces") is None:
        bien["pieces"] = _parse_int_simple(fields.get("nb pièces", ""))
    if bien.get("chambres") is None:
        bien["chambres"] = _parse_int_simple(fields.get("chambres", ""))
    if not bien.get("surface"):
        bien["surface"] = _parse_float(fields.get("surface habitable", ""))
    ref = fields.get("référence", "").strip()
    if ref:
        bien["id_annonce"] = ref

    # Description : meta description / og:description
    desc = ""
    md = soup.find("meta", {"name": "description"})
    if md and md.get("content"):
        desc = md["content"].strip()
    if not desc:
        og = soup.find("meta", {"property": "og:description"})
        if og and og.get("content"):
            desc = og["content"].strip()
    bien["description"] = desc[:1200]

    return bien


def _extract_fields(soup) -> dict:
    fields: dict[str, str] = {}
    for li in soup.select("li"):
        st = li.find("strong")
        if not st:
            continue
        label = st.get_text(strip=True).lower()
        # valeur = texte du li sans le label
        full = li.get_text(" ", strip=True)
        val = full[len(st.get_text(strip=True)):].strip()
        if label and val:
            fields[label] = val
    return fields


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_ville(text: str) -> tuple[str, str]:
    """'66440 TORREILLES' → ('66440', 'TORREILLES')."""
    if not text:
        return "", ""
    m = re.search(r"\b(\d{5})\b", text)
    cp = m.group(1) if m else ""
    ville = re.sub(r"\b\d{5}\b", "", text).strip(" -,").strip()
    return cp, ville


def _parse_price(text: str) -> float | None:
    """'755.000 €' / '1.025.000 €' → float (point = séparateur de milliers)."""
    if not text:
        return None
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d.,]", "", cleaned)
    # Le site utilise le point comme séparateur de milliers, pas de décimales prix.
    cleaned = cleaned.replace(".", "").replace(",", "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_float(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0]*[.,]?\d*)", text)
    if not m:
        return None
    val = m.group(1).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        f = float(val)
        return f if f > 0 else None
    except ValueError:
        return None


def _parse_int_simple(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


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
    print(f"\nTotal Walter & de Maison : {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

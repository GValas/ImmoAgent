"""scrapers/adressimmo.py — Adressimmo (Century 21 Châteauroux / Indre)

Méthode : scrape_simple (httpx) — SSR HTML (CMS ImmoFacile / WebGenery)

Agence locale mono-secteur : tout l'inventaire est dans l'Indre (36) avec
quelques biens débordant sur le Cher (18) limitrophe. Il n'y a donc PAS de
filtre département côté serveur (inutile pour une agence locale) → on scrape
l'intégralité du catalogue puis on POST-FILTRE strictement sur code_postal[:2].

URL liste : /fr/ventes  (1ʳᵉ page = 20 cartes, pose le cookie de session de
            recherche "SEformSearchTransaction").
Pagination : route FOSJsRouting "SiteEngineMapMap_miniFiche" →
            /fr/map/mini-fiche/{metier}/{start}/{typeBien}
            (ex: /fr/map/mini-fiche/Transaction/20/normal), chargée en AJAX au
            scroll, par paquets de 20. NÉCESSITE le cookie de session posé par
            /fr/ventes au préalable (sinon réponse vide).

Cartes : div.article[data-uuid]
  - URL   : a[href]  → /fr/vente/{type}-{npieces}-{ville}-{CP}/{uuid}
            → CP (5 chiffres) extrait directement du slug d'URL : "...-36260/..."
  - Alt img: "Acheter Maison 6 pièces 250 m² Reuilly 36260"
            → type, nb pièces, surface habitable, ville, CP (source la + fiable)
  - Titre : h3 > a            → "Maison - 6 pièces \n Reuilly"
  - Desc  : p.desc
  - Réf   : p.reference       → "Réf. : 12438"  (id_annonce)
  - Prix  : p.prix_bien .prix → "35 000 € HAI"
  - Photo : span.cover_bien img[src]  (1 photo cover par carte)

Type de bien : on ne garde que les maisons / propriétés (exclut appartements,
               terrains, locaux, parkings, immeubles).

Couverture : Indre (36) ~110 biens, Cher (18) ~2. AUCUN bien hors zone locale.
             → pour les départements cibles 72/28/45/89, ce scraper renvoie 0
             (normal : agence locale 36/18). Conservé actif pour 36/18.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.adressimmo.fr"
LISTING_URL = f"{BASE_URL}/fr/ventes"
PAGE_SIZE = 20
MAX_BIENS = 400  # garde-fou (catalogue ~130)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien à conserver (segment de slug / alt) : maisons / propriétés…
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme|"
    r"maison-de-village|propriete-de-caractere",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|cave|box",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Agence locale 36/18 : si aucun dept cible n'est dans son périmètre,
    # inutile d'interroger le site.
    if departements and not (set(departements) & {"36", "18"}):
        print("[Adressimmo] Aucun dept cible dans le périmètre 36/18 → skip")
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        # 1ʳᵉ page : pose le cookie de session de recherche + 1ᵉʳ paquet de cartes.
        try:
            r = await client.get(LISTING_URL)
        except Exception as e:
            print(f"[Adressimmo] Erreur accès listing: {e}")
            return []
        if r.status_code != 200:
            print(f"[Adressimmo] Listing status {r.status_code}")
            return []

        pages = [r.text]

        # Pagination AJAX (route FOSJsRouting) par paquets de 20.
        start = PAGE_SIZE
        while start < MAX_BIENS:
            url = f"{BASE_URL}/fr/map/mini-fiche/Transaction/{start}/normal"
            await asyncio.sleep(0.5)
            try:
                rp = await client.get(
                    url, headers={**HEADERS, "X-Requested-With": "XMLHttpRequest",
                                  "Referer": LISTING_URL}
                )
            except Exception as e:
                print(f"[Adressimmo] Erreur page start={start}: {e}")
                break
            if rp.status_code != 200 or len(rp.text.strip()) < 50:
                break
            if not BeautifulSoup(rp.text, "html.parser").select("div.article"):
                break
            pages.append(rp.text)
            start += PAGE_SIZE

        for html in pages:
            for card in BeautifulSoup(html, "html.parser").select("div.article"):
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                cp = bien["code_postal"]
                # Post-filtre département STRICT (0 fuite hors-zone).
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

    from collections import Counter
    dist = Counter(b["departement"] for b in results)
    print(f"[Adressimmo] {len(results)} annonces — répartition {dict(dist)}")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # CP depuis le slug d'URL : /fr/vente/maison-6-pieces-reuilly-36260/{uuid}
    cp_m = re.search(r"-(\d{5})/", href)
    code_postal = cp_m.group(1) if cp_m else ""

    # Alt de la photo cover : "Acheter Maison 6 pièces 250 m² Reuilly 36260"
    img = card.select_one("span.cover_bien img") or card.select_one("img")
    alt = img.get("alt", "") if img else ""

    # CP de secours via l'alt
    if not code_postal:
        m = re.search(r"(\d{5})\s*$", alt.strip())
        if m:
            code_postal = m.group(1)

    # Type de bien : 1ᵉʳ segment du slug d'URL UNIQUEMENT.
    # (slug = "{type}-{npieces}-{ville}-{CP}" ; on ne lit que le type pour ne pas
    #  attraper un nom de ville contenant "chateau", "mas", etc.)
    slug = href.split("/")[-2] if "/" in href else href
    type_seg = slug.split("-", 1)[0]  # "appartement", "maison", "propriete"…
    # On lit aussi le 1ᵉʳ mot de l'alt comme garde-fou ("Acheter Appartement …").
    alt_type = ""
    am0 = re.match(r"\s*Acheter\s+(\w+)", alt, re.IGNORECASE)
    if am0:
        alt_type = am0.group(1).lower()
    if _EXCLUDE_TYPE.search(type_seg) or (alt_type and _EXCLUDE_TYPE.search(alt_type)):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg.replace("-", " ").strip() or "maison"

    # Pièces & surface depuis l'alt : "... 6 pièces 250 m² ..."
    pieces = None
    pm = re.search(r"(\d+)\s*pi[eè]ces?", alt, re.IGNORECASE)
    if pm:
        pieces = int(pm.group(1))
    else:
        # formats f1/t3 dans le slug
        fm = re.search(r"[ft](\d+)", slug, re.IGNORECASE)
        if fm:
            pieces = int(fm.group(1))

    surface = None
    sm = re.search(r"([\d.,]+)\s*m²", alt)
    if sm:
        try:
            surface = float(sm.group(1).replace(",", "."))
        except ValueError:
            surface = None

    # Ville : depuis h3, sinon depuis l'alt
    h3 = card.select_one("h3")
    ville = ""
    if h3:
        lines = [t.strip() for t in h3.get_text("\n").split("\n") if t.strip()]
        if lines:
            ville = lines[-1]
    if not ville and alt:
        am = re.search(r"m²\s+(.+?)\s+\d{5}\s*$", alt)
        if am:
            ville = am.group(1).strip()

    # Titre
    titre = ""
    if h3:
        titre = " ".join(h3.get_text(" ", strip=True).split())
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Description
    desc_el = card.select_one("p.desc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Référence (id_annonce)
    ref_el = card.select_one("p.reference")
    ref = ""
    if ref_el:
        rm = re.search(r"R[ée]f\.?\s*:?\s*(\S+)", ref_el.get_text(" ", strip=True))
        if rm:
            ref = rm.group(1)
    uuid = card.get("data-uuid") or ""
    id_annonce = ref or uuid or url

    # Prix
    price_el = card.select_one("p.prix_bien .prix") or card.select_one("p.prix_bien")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Photo cover
    photos = []
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)

    return {
        "source": "adressimmo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else None,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Century 21 Adressimmo",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    # retire la mention "Prix de vente :" et "HAI"
    text = re.sub(r"prix de vente\s*:?", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
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
    print(f"\nTotal Adressimmo: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

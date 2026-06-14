"""scrapers/_ac3_immo.py — Socle commun pour agences sur le CMS « AC3 / immo-facile ».

Plusieurs agences locales d'Eure-et-Loir partagent exactement le même CMS
(éditeur AC3 Groupe, gabarit dit « immo-facile/modelo ») : pages de vente
SSR servies en HTML pur, mêmes sélecteurs, même schéma de pagination.

  - Listing national de l'agence : /annonces/transaction/Vente.html
  - Pagination               : /annonces/transaction_____{N}/vente.html (5 _)
  - Carte                     : div.cell-product
      • .product-name          → titre
      • .product-localisation  → « VILLE (CP) »  → ville + code_postal
      • .product-short-infos   → « N pièce(s) / NNN m² »
      • .product-price         → prix
      • .product-ref           → « Ref : XXXX »   → id_annonce
      • a[href*='/fiches/']    → URL détail
      • img.photo              → photo (src relatif ../office.../...)

Comme la liste n'est PAS filtrée par département côté serveur, on extrait le CP
de chaque carte (présent en clair « (28320) ») et on POST-FILTRE strictement
sur code_postal[:2] ∈ départements cibles → 0 fuite garantie.

Interface réutilisée par les scrapers fins (maintenon, chaumiere, apally…) :
    biens = await search_ac3(criteres, base_url="https://...", source="id",
                             label="Nom", agence="Nom agence")
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_int, parse_loc, parse_price
from scrapers._geo_resolve import resolve_dept

MAX_PAGES = 12
PHOTOS_PER_CARD = 1

_TYPE_RE = re.compile(r"maison|propri[eé]t[eé]|longère|longere|ferme|manoir|moulin|demeure|château|chateau|villa", re.I)


def _parse_card(card, base_url: str, source: str, agence: str) -> dict | None:
    link = card.select_one("a[href*='/fiches/']")
    href = link.get("href") if link else None
    if not href:
        return None
    url = urljoin(base_url + "/annonces/transaction/", href)

    name_el = card.select_one(".product-name")
    titre = name_el.get_text(" ", strip=True) if name_el else ""

    # Deux variantes du gabarit AC3 :
    #  - récente : div.product-localisation = « VILLE (28320) »  → CP présent ;
    #  - ancienne : pas de localisation, la ville est suffixée au titre après une
    #    virgule (« …136 m² , Maintenon ») et AUCUN CP → résolu via geo.api.gouv.fr.
    loc_el = card.select_one(".product-localisation")
    if loc_el:
        ville, code_postal = parse_loc(loc_el.get_text(" ", strip=True))
    else:
        code_postal = ""
        ville = ""
        if "," in titre:
            ville = titre.rsplit(",", 1)[-1].strip()
            titre = titre.rsplit(",", 1)[0].strip()

    short_el = card.select_one(".product-short-infos")
    short = short_el.get_text(" ", strip=True) if short_el else ""
    pieces = parse_int(r"(\d+)\s*pi[eè]ce", short)
    surface = None
    m = re.search(r"([\d\s\xa0]+)\s*m²", short)
    if m:
        try:
            surface = float(re.sub(r"[\s\xa0]", "", m.group(1)))
        except ValueError:
            surface = None

    # Le prix peut être suivi d'un span « dont X% TTC d'honoraires » → on isole
    # le 1er nœud texte (le montant) pour ne pas concaténer le pourcentage.
    prix = None
    price_el = card.select_one(".product-price")
    if price_el:
        for sub in price_el.select(".price_honoraires_acquereur"):
            sub.extract()
        prix = parse_price(price_el.get_text(" ", strip=True))

    ref_el = card.select_one(".product-ref")
    ref = ""
    if ref_el:
        ref = re.sub(r"^\s*Ref\s*:\s*", "", ref_el.get_text(" ", strip=True), flags=re.I)
    id_annonce = ref or url

    # type de bien depuis le titre (filtre maison/propriété…)
    type_bien = "maison"
    mt = _TYPE_RE.search(titre)
    if mt:
        type_bien = mt.group(0).lower()

    photos = []
    img = card.select_one("img.photo, .product-image img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            photos.append(urljoin(base_url + "/annonces/transaction/", src))

    return {
        "source": source,
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": agence,
    }


async def search_ac3(
    criteres: dict, *, base_url: str, source: str, label: str, agence: str,
) -> list[dict]:
    base_url = base_url.rstrip("/")
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()   # tous les biens vus (fin de pagination)
    kept_ids: set[str] = set()   # biens retenus (dédup résultats)
    geo_cache: dict[str, tuple[str, str]] = {}

    async with make_client() as client:
        for page in range(1, MAX_PAGES + 1):
            if page == 1:
                url = f"{base_url}/annonces/transaction/Vente.html"
            else:
                url = f"{base_url}/annonces/transaction_____{page}/vente.html"
            r = await get_with_retry(client, url)
            if r is None or r.status_code != 200:
                break
            cards = BeautifulSoup(r.text, "html.parser").select("div.cell-product")
            if not cards:
                break

            new_cards = 0
            kept = 0
            for card in cards:
                try:
                    bien = _parse_card(card, base_url, source, agence)
                except Exception:
                    continue
                if not bien:
                    continue
                aid = bien.get("id_annonce")
                if aid not in seen_ids:
                    seen_ids.add(aid)
                    new_cards += 1
                cp = bien.get("code_postal") or ""
                # Variante sans CP sur la carte → résolution commune→(dept,CP)
                # via geo.api.gouv.fr (cache) avant le post-filtre.
                if not cp and bien.get("ville"):
                    dept_r, cp_r = await resolve_dept(client, bien["ville"], geo_cache)
                    if cp_r:
                        cp = cp_r
                        bien["code_postal"] = cp_r
                        bien["departement"] = dept_r or cp_r[:2]
                # POST-FILTRE DÉPARTEMENT STRICT (0 fuite)
                if not cp or cp[:2] not in departements:
                    continue
                if aid in kept_ids:
                    continue
                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue
                kept_ids.add(aid)
                results.append(bien)
                kept += 1

            print(f"[{label}] Page {page}: {len(cards)} cartes ({new_cards} nouvelles), {kept} retenues (cumul {len(results)})")
            # On arrête quand plus aucune carte inédite n'arrive (pagination épuisée),
            # pas quand 0 bien retenu : un match peut être en page suivante.
            if new_cards == 0 and page > 1:
                break
            await asyncio.sleep(0.5)

    print(f"[{label}] Total {len(results)} annonces (départements cibles)")
    return results

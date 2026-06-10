"""scrapers/lagenceauxerroise.py — L'Agence Auxerroise (agence locale, Auxerre 89)

Méthode : scrape_simple (httpx) — SSR HTML (moteur Immo-Facile / Gimi).

URL liste : /annonces/transaction/vente.html              (page 1)
            /annonces/transaction_____{n}/vente.html       (pages 2..n)
  → agence mono-département (Auxerre), tout est dans l'Yonne (89), mais on
    NE suppose RIEN : on lit le code postal réel sur la page détail de CHAQUE
    bien et on applique un post-filtre STRICT code_postal[:2] ∈ departements
    cibles → 0 fuite hors-zone.

Cartes liste : div.item-product
  - Lien détail : .visuel-product a[href] / .products-link a[href]
                  → ../fiches/{slug}_{id}/{titre}.html
  - Titre       : .products-name
  - Description  : .products-desc
  - Réf         : .products-ref     → "Ref. : 3619"
  - Prix        : .products-price   → "209 000 €"
  - Photo       : .visuel-product img.photo[src]

Page détail (clés/valeurs en lignes div.row > div.col-sm-6) :
  Code postal, Ville, Surface ("160 m2"), Surface terrain, Nombre pièces,
  Chambres, Consommation énergie finale (= classe DPE A..G), photos galerie.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.lagenceauxerroise.fr"
MAX_PAGES = 10
PHOTOS_PER_CARD = 12

# Départements cibles (sécurité supplémentaire ; la liste réelle vient de criteres)
DEPTS_CIBLES = {"72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    departements &= DEPTS_CIBLES
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            if page == 1:
                url = f"{BASE_URL}/annonces/transaction/vente.html"
            else:
                url = f"{BASE_URL}/annonces/transaction_____{page}/vente.html"

            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[AgenceAuxerroise] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.item-product")
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                try:
                    base = _parse_card(card)
                except Exception:
                    continue
                if not base:
                    continue
                if base["id_annonce"] in seen_ids:
                    continue
                seen_ids.add(base["id_annonce"])

                # Enrichissement détail : code postal/ville/surface/DPE authentiques
                try:
                    bien = await _enrich_detail(client, base)
                except Exception:
                    bien = base

                # Post-filtre département STRICT (0 fuite hors-zone)
                cp = bien.get("code_postal") or ""
                if not cp or cp[:2] not in departements:
                    continue
                bien["departement"] = cp[:2]

                # Bornes prix / surface (sans exclure les champs manquants)
                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                results.append(bien)
                new_on_page += 1

            if new_on_page == 0:
                # plus rien de neuf (dernière page clampée) → on arrête
                if page > 1:
                    break

            await asyncio.sleep(0.6)

    print(f"[AgenceAuxerroise] Total: {len(results)} annonces")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one(".visuel-product a[href]") or card.select_one(
        ".products-link a[href]"
    )
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = _abs_url(href)

    # id annonce : numéro dans le slug ../fiches/{...}_{id}/...
    m = re.search(r"_(\d+)/", href)
    id_num = m.group(1) if m else ""

    ref_el = card.select_one(".products-ref")
    ref = ""
    if ref_el:
        rm = re.search(r"Ref\.?\s*:?\s*(\S+)", ref_el.get_text(" ", strip=True))
        ref = rm.group(1) if rm else ""
    id_annonce = ref or id_num or url

    name_el = card.select_one(".products-name")
    titre = name_el.get_text(" ", strip=True) if name_el else ""

    # exclusion par type évident dans le titre
    if _EXCLUDE_TYPE.search(titre):
        return None

    desc_el = card.select_one(".products-desc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    price_el = card.select_one(".products-price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    type_bien = _guess_type(titre)

    img = card.select_one(".visuel-product img")
    photos = []
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(_abs_url(src))

    return {
        "source": "lagenceauxerroise",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": "",
        "ville": "",
        "code_postal": "",
        "surface": None,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "L'Agence Auxerroise",
    }


async def _enrich_detail(client: httpx.AsyncClient, bien: dict) -> dict:
    r = await client.get(bien["url"])
    if r.status_code != 200:
        return bien
    soup = BeautifulSoup(r.text, "html.parser")

    fields = _detail_fields(soup)

    cp = fields.get("code postal", "")
    cpm = re.search(r"\d{5}", cp)
    if cpm:
        bien["code_postal"] = cpm.group(0)

    ville = fields.get("ville", "")
    if ville:
        bien["ville"] = ville[:80]

    if bien.get("surface") is None:
        bien["surface"] = _num(fields.get("surface", ""))
    if bien.get("surface_terrain") is None:
        bien["surface_terrain"] = _num(fields.get("surface terrain", ""))
    if bien.get("pieces") is None:
        bien["pieces"] = _int(fields.get("nombre pièces", ""))
    if bien.get("chambres") is None:
        bien["chambres"] = _int(fields.get("chambres", ""))

    # DPE = classe énergie finale (A..G)
    dpe_raw = fields.get("consommation énergie finale", "")
    dm = re.search(r"\b([A-G])\b", dpe_raw)
    if dm:
        bien["dpe"] = dm.group(1)

    # Prix de secours si absent en liste
    if not bien.get("prix"):
        bien["prix"] = _parse_price(fields.get("prix", ""))

    # Photos galerie détail
    gallery = []
    for im in soup.select("img.photo, img[itemprop='image']"):
        src = im.get("src") or im.get("data-src") or ""
        if src and not src.startswith("data:"):
            gallery.append(_abs_url(src))
    if gallery:
        merged = list(dict.fromkeys(bien.get("photos", []) + gallery))
        bien["photos"] = merged[:PHOTOS_PER_CARD]

    return bien


def _detail_fields(soup) -> dict[str, str]:
    """Lit les lignes label/valeur 'div.row > div.col-sm-6' de la fiche."""
    out: dict[str, str] = {}
    for row in soup.select("div.row"):
        cols = row.select("div.col-sm-6")
        if len(cols) != 2:
            continue
        lab = cols[0].get_text(" ", strip=True).lower()
        val = cols[1].get_text(" ", strip=True)
        if lab and val and len(lab) < 40 and lab not in out:
            out[lab] = val
    return out


# ── Helpers ──────────────────────────────────────────────────────────────────

def _abs_url(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return BASE_URL + "/" + href.lstrip("./").lstrip("/")


def _guess_type(titre: str) -> str:
    t = titre.lower()
    for kw in (
        "longère",
        "longere",
        "ferme",
        "manoir",
        "château",
        "chateau",
        "moulin",
        "propriété",
        "propriete",
        "domaine",
        "maison",
        "villa",
        "pavillon",
    ):
        if kw in t:
            return kw.replace("longere", "longère").replace("propriete", "propriété")
    return "maison"


def _parse_price(text: str) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"(?i)eur|€", "", text)
    cleaned = re.sub(r"[\s\xa0]", "", cleaned)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _num(text: str) -> float | None:
    """'160 m2' / '1 140 m2' → float."""
    if not text:
        return None
    m = re.search(r"([\d\s\xa0]+)", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
        return f if f > 0 else None
    except ValueError:
        return None


def _int(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


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
    print(f"\nTotal Agence Auxerroise: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — DPE {b.get('dpe') or '?'}"
            f" — {b['type_bien']} — {b['ville']}"
        )

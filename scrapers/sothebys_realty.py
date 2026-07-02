"""
scrapers/sothebys_realty.py — Sotheby's International Realty France (prestige)
Méthode : scrape_simple (httpx) — moteur POST SSR (réactivé 2026-07-02).

L'ancien domaine sothebysrealty.fr n'existe plus ; le vrai portail national est
sothebysrealty-france.com (SSR, pas d'anti-bot). Recette vérifiée :
  - POST /fr/annonces/ avec form_post=1, geo_multi[]=FR;{dept} ET
    flagval_search_city="FR;{dept}|label|lat,lng" (sans flagval le filtre géo est
    ignoré), + filtres serveur min/max (prix) et surface → 302 vers
    /fr/immobilier-luxe/ rendu SSR (l'état de recherche est en session/cookies →
    session neuve par département).
  - Résultats RÉELS dans div.ctn_article_annonce > div.annonce ; en zone vide le
    site rend ctn_article_noresult + carrousel .similarListing (annonce.swiper-slide,
    autres départements) qu'il faut EXCLURE.
  - CP réel dans le slug détail (…-6-pieces-89000/) → garde-fou dept vérifiable.
    IDs géo relevés via /lib/http_request/city_search.php?q=… (« FR;72 » etc.).
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import _jitter, keep_bien, make_client, parse_float, parse_int

BASE = "https://www.sothebysrealty-france.com"

# Libellés + coords indicatives (chef-lieu) pour flagval_search_city.
_DEPTS = {
    "72": ("Sarthe", 47.99, 0.19), "28": ("Eure-et-Loir", 48.44, 1.48),
    "45": ("Loiret", 47.90, 2.10), "89": ("Yonne", 47.80, 3.57),
    "49": ("Maine-et-Loire", 47.47, -0.55), "37": ("Indre-et-Loire", 47.39, 0.69),
    "36": ("Indre", 46.81, 1.69), "18": ("Cher", 47.08, 2.40),
    "58": ("Nièvre", 47.00, 3.16), "41": ("Loir-et-Cher", 47.59, 1.33),
    "53": ("Mayenne", 48.07, -0.77),
}

_SKIP_TYPES = ("appartement", "immeuble", "terrain", "commerce", "bureau", "parking")


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a[href*='/immobilier-luxe/ref-']")
    if not link:
        return None
    url = link["href"]
    if not url.startswith("http"):
        url = BASE + url

    m = re.search(r"/ref-([a-z0-9-]+)/[^/]*-(\d{5})/?$", url)
    ref = m.group(1) if m else url.rstrip("/").split("/")[-1]
    cp = m.group(2) if m else ""

    type_el = card.select_one("span.type")
    type_txt = (type_el.get_text(strip=True) if type_el else "maison").lower()
    if any(t in type_txt for t in _SKIP_TYPES):
        return None

    price_el = card.select_one("span.price")
    prix = parse_float(r"([\d\s\xa0]{4,})\s*€",
                       (price_el.get_text(strip=True) if price_el else "").replace("\xa0", " "))
    if not prix or prix < 10_000:        # « Prix sur demande »
        return None

    ville_el = card.select_one("span.city")
    ville = ville_el.get_text(strip=True) if ville_el else ""
    surf_el = card.select_one("span.surface")
    surface = parse_float(r"([\d\s]+(?:[.,]\d+)?)\s*m²",
                          (surf_el.get_text(strip=True) if surf_el else "").replace("\xa0", " "))
    pieces_el = card.select_one("span.pieces")
    pieces = parse_int(r"(\d+)", pieces_el.get_text(strip=True) if pieces_el else "")

    photos = []
    for img in card.select("img"):
        src = img.get("data-src") or img.get("src") or ""
        if src and "datas/biens" in src:
            photos.append(src if src.startswith("http") else BASE + src)

    return {
        "source": "sothebys_realty",
        "url": url,
        "id_annonce": ref,
        "titre": f"Vente {type_txt.title()} {ville}"[:150],
        "type_bien": "chateau" if "château" in type_txt else "maison",
        "description": card.get_text(" ", strip=True)[:600],
        "departement": cp[:2] if cp else dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos[:10],
        "dpe": None,
        "agence": "Sotheby's International Realty",
    }


def _real_cards(html: str) -> list:
    """Cartes du bloc résultats — exclut le carrousel « biens similaires »
    (rendu quand 0 résultat, avec des biens d'AUTRES départements)."""
    soup = BeautifulSoup(html, "html.parser")
    cards = []
    for cont in soup.select("div.ctn_article_annonce"):
        for c in cont.select("div.annonce"):
            if "swiper-slide" in (c.get("class") or []):
                continue
            cards.append(c)
    return cards


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = int(criteres.get("prix_max") or 0)
    prix_min = int(criteres.get("prix_min") or 0)
    surface_min = int(criteres.get("surface_min") or 0)

    results: list[dict] = []
    seen_ids: set = set()
    for dept in departements:
        if dept not in _DEPTS:
            continue
        label, lat, lng = _DEPTS[dept]
        data = {
            "form_post": "1",
            "geo_multi[]": f"FR;{dept}",
            "flagval_search_city": f"FR;{dept}|{label} ({dept})|{lat},{lng}",
        }
        if prix_min:
            data["min"] = str(prix_min)
        if prix_max:
            data["max"] = str(prix_max)
        if surface_min:
            data["surface"] = str(surface_min)

        kept = 0
        try:
            # Client (cookies) NEUF par département : l'état de recherche est en session.
            async with make_client(timeout=30) as client:
                r = await client.post(f"{BASE}/fr/annonces/", data=data)
                if r.status_code != 200:
                    print(f"[SothebysRealty] Dept {dept}: HTTP {r.status_code}")
                    continue
                for card in _real_cards(r.text):
                    try:
                        bien = _parse_card(card, dept)
                    except Exception:
                        continue
                    if bien and keep_bien(bien, dept, seen_ids,
                                          prix_max=prix_max, prix_min=prix_min,
                                          surface_min=surface_min):
                        results.append(bien)
                        kept += 1
            print(f"[SothebysRealty] Dept {dept}: {kept} annonces")
        except Exception as e:
            print(f"[SothebysRealty] Erreur dept {dept}: {e}")
        await asyncio.sleep(_jitter(2.0))

    return results


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "SothebysRealty")

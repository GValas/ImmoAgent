"""
scrapers/barnes.py — BARNES International (prestige) — réactivé 2026-07-02.
Méthode : scrape_simple (httpx) — pages département SSR + AJAX de pagination.

L'ancien domaine barnes-immobilier.com était faux. Sur barnes-international.com :
  - /fr/vente/france/{dept-slug}.html est SSR (cartes <article id="property-REF">)
    et FILTRE par département côté serveur (vérifié : communes du dept uniquement).
    Les 11 slugs cibles existent (12-31 annonces/dept, beaucoup hors budget).
  - La page pose une session (cookies) ; la suite se charge via
    /views/viewAjax.php?view=viewListing_annonces&ajax=y&action=annonces_suivantes
    &begin=N&type_moteur=listing → cartes HTML supplémentaires, « nodata » à la fin.
    → flux SÉQUENTIEL par département (la session porte la localisation).
  - Cartes sans code postal (ville seule) → departement = dept requêté, CP vide.
    a.bc-015-content-link, p.mb-2 = ville, p.bc-015-criteria (chambres, m²,
    « Surface extérieure » = terrain), p.bc-015-prix, img[data-src].
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import (
    DEFAULT_DEPT_SLUGS,
    _jitter,
    get_with_retry,
    keep_bien,
    make_client,
    parse_float,
    parse_int,
)

BASE = "https://www.barnes-international.com"
AJAX = (BASE + "/views/viewAjax.php?view=viewListing_annonces&ajax=y"
        "&action=annonces_suivantes&begin={begin}&type_moteur=listing")

MAX_BIENS_PER_DEPT = 96          # garde-fou pagination AJAX
_SKIP_TYPES = ("appartement", "penthouse", "studio", "duplex", "bureau",
               "commerce", "parking", "terrain")


def _parse_card(card, dept: str) -> dict | None:
    ref = (card.get("id") or "").replace("property-", "")
    link = card.select_one("a[href*='/ref-']")
    if not ref or not link:
        return None
    url = link["href"]
    if not url.startswith("http"):
        url = BASE + url

    title_txt = " ".join(link.get_text(" ", strip=True).split())
    m = re.search(r"À vendre\s+([^|]+)\|\s*(.+?)(?:\s{2,}|$)", title_txt)
    type_txt = (m.group(1).strip() if m else "Maison").lower()
    if any(t in type_txt for t in _SKIP_TYPES):
        return None

    prix_el = card.select_one("p.bc-015-prix")
    prix = parse_float(r"([\d\s\xa0]{4,})\s*€",
                       (prix_el.get_text(" ", strip=True) if prix_el else "").replace("\xa0", " "))
    if not prix or prix < 10_000:        # « Prix sur demande »
        return None

    ville_el = card.select_one("p.mb-2")
    ville = ville_el.get_text(" ", strip=True) if ville_el else ""

    crit_el = card.select_one("p.bc-015-criteria")
    crit = (crit_el.get_text(" ", strip=True) if crit_el else "").replace("\xa0", " ")
    terrain = parse_float(r"Surface extérieure\s*([\d\s]+)\s*m²", crit)
    crit_sans_terrain = re.sub(r"Surface extérieure\s*[\d\s]+\s*m²", "", crit)
    surface = parse_float(r"([\d\s]+(?:[.,]\d+)?)\s*m²", crit_sans_terrain)
    chambres = parse_int(r"(\d+)\s*chambres?", crit)

    photos = []
    for img in card.select("img[data-src], img[src]"):
        src = img.get("data-src") or img.get("src") or ""
        if src.startswith("http") and "no-picture" not in src and src not in photos:
            photos.append(src)

    return {
        "source": "barnes",
        "url": url,
        "id_annonce": ref,
        "titre": f"À vendre {type_txt.title()} | {ville}"[:150],
        "type_bien": "chateau" if "château" in type_txt else "maison",
        "description": crit,
        "departement": dept,     # page département serveur fiable ; pas de CP en carte
        "ville": ville[:80],
        "code_postal": "",
        "surface": surface,
        "surface_terrain": terrain,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos[:10],
        "dpe": None,
        "agence": "Barnes",
    }


def _cards(html: str, main_only: bool = False):
    """Cartes annonce. `main_only` restreint au conteneur #content_annonces :
    la page département SSR ajoute après lui une section « Biens à proximité »
    (départements voisins) qu'il faut EXCLURE (sinon fuite de département)."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#content_annonces") if main_only else soup
    return root.select("article[id^='property-']") if root else []


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set = set()        # global : les biens frontaliers apparaissent 2×
    async with make_client(timeout=25) as client:
        for dept in departements:
            slug = DEFAULT_DEPT_SLUGS.get(dept)
            if not slug:
                continue
            kept = 0
            try:
                # 1) Page département SSR — pose aussi la session pour l'AJAX.
                r = await get_with_retry(client, f"{BASE}/fr/vente/france/{slug}.html")
                if r is None or r.status_code != 200:
                    print(f"[Barnes] Dept {dept}: HTTP {r.status_code if r else 'ERR'}")
                    continue
                cards = _cards(r.text, main_only=True)
                begin = len(cards)

                # 2) Pagination AJAX session (« nodata » à épuisement).
                while cards:
                    for card in cards:
                        try:
                            bien = _parse_card(card, dept)
                        except Exception:
                            continue
                        if bien and keep_bien(bien, dept, seen_ids,
                                              prix_max=prix_max, prix_min=prix_min,
                                              surface_min=surface_min):
                            results.append(bien)
                            kept += 1
                    if begin >= MAX_BIENS_PER_DEPT:
                        break
                    await asyncio.sleep(_jitter(1.5))
                    r = await get_with_retry(client, AJAX.format(begin=begin))
                    if r is None or r.status_code != 200 or r.text.strip() == "nodata":
                        break
                    cards = _cards(r.text)
                    begin += len(cards)
                print(f"[Barnes] Dept {dept}: {kept} annonces")
            except Exception as e:
                print(f"[Barnes] Erreur dept {dept}: {e}")
            await asyncio.sleep(_jitter(2.0))

    return results


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Barnes")

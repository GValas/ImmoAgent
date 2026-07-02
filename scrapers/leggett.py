"""
scrapers/leggett.py — Leggett Immobilier (réseau national, clientèle internationale)
Méthode : Playwright + BeautifulSoup (réécrit 2026-07-02)
leggett.fr redirige vers leggett-immo.com, derrière un challenge Cloudflare JS :
httpx est bloqué (403) même avec cookie cf_clearance (lié au fingerprint TLS),
mais Chromium headless passe le challenge. Recherche multi-départements en UNE
requête via segments d'URL CakePHP :
  /acheter-vendre-une-maison/mainSearch/departments:72--28--…/min_price:…/max_price:…/min_habitable:…/page:N
Pas de code postal dans les cartes — département fiabilisé par le suffixe de la
référence (A45776SGI72 → 72) recoupé avec la liste demandée.
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from scrapers._base import parse_price_digits

BASE = "https://www.leggett-immo.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
MAX_PAGES = 10  # ~46 cartes/page

# Nom de département tel qu'il apparaît dans le slug des annonces (accents inclus)
_DEPT_NAMES = {
    "72": "sarthe", "28": "eure-et-loir", "45": "loiret", "89": "yonne",
    "49": "maine-et-loire", "37": "indre-et-loire", "36": "indre", "18": "cher",
    "58": "nièvre", "41": "loir-et-cher", "53": "mayenne",
}


def _parse_card(card, depts: set[str]) -> dict | None:
    link = card.select_one("a[href*='/view/']")
    ref_el = card.select_one(".result-item-ref")
    if not link or not ref_el:
        return None
    href = link.get("href", "")
    url = href if href.startswith("http") else BASE + href
    m = re.search(r"/view/([A-Z0-9]+)/", href)
    ref = m.group(1) if m else ""
    # Département = suffixe numérique de la référence (garde-fou anti-fuite)
    m = re.search(r"(\d{2})$", ref)
    dept = m.group(1) if m else ""
    if dept not in depts:
        return None

    # 1er montant € seulement : les biens « prix réduit » affichent 2 prix
    # (nouveau puis ancien) — les concaténer donnerait un prix aberrant
    prix_el = card.select_one(".result-price")
    m = re.search(r"(\d[\d\s\xa0]*)€", prix_el.get_text() if prix_el else "")
    prix = parse_price_digits(m.group(1)) if m else None
    if not prix or prix < 10_000:
        return None

    # slug : /view/{ref}/maison-a-vendre-a-lombron-sarthe-pays de la loire-france
    ville, type_bien = "", "maison"
    m = re.search(r"/view/[A-Z0-9]+/([a-zà-ÿ]+)-a-vendre-a-(.+?)-france\s*$", href)
    if m:
        type_bien = m.group(1)
        reste = m.group(2)
        nom_dept = _DEPT_NAMES.get(dept, "")
        ville = reste.split(f"-{nom_dept}-")[0] if nom_dept and f"-{nom_dept}-" in reste \
            else reste.split("-")[0]
        ville = ville.replace("-", " ").title()

    carac = " ".join(el.get_text(" ", strip=True)
                     for el in card.select(".characteristics-item"))
    surface = None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*m\s*2?\b", carac)
    if m:
        surface = float(m.group(1).replace(",", "."))
    pieces = None
    m = re.search(r"(\d+)\s*pièces?", carac)
    if m:
        pieces = int(m.group(1))
    chambres = None
    m = re.search(r"(\d+)\s*chambres?", carac)
    if m:
        chambres = int(m.group(1))
    terrain = None
    m = re.search(r"([\d\s\xa0]+)\s*m\s*2?\s*de\s*terrain", carac)
    if m:
        terrain = parse_price_digits(m.group(1))

    titre_el = card.select_one(".result-item-description")
    desc_el = card.select_one(".result-item-description-highlighted")
    img = card.select_one("img.result-item-visual-image")
    return {
        "source": "leggett",
        "url": url,
        "id_annonce": ref,
        "titre": (titre_el.get_text(strip=True) if titre_el else f"Maison {ville}")[:150],
        "type_bien": type_bien,
        "description": (desc_el.get_text(" ", strip=True) if desc_el else "")[:1200],
        "departement": dept,
        "ville": ville,
        "code_postal": "",          # non exposé en vue liste
        "surface": surface,
        "surface_terrain": terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": [img["src"]] if img and img.get("src", "").startswith("http") else [],
        "dpe": None,
        "agence": "Leggett Immobilier",
    }


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    if not departements:
        return []
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    path = f"{BASE}/acheter-vendre-une-maison/mainSearch/departments:" + "--".join(departements)
    if prix_min:
        path += f"/min_price:{int(prix_min)}"
    if prix_max:
        path += f"/max_price:{int(prix_max)}"
    if surface_min:
        path += f"/min_habitable:{int(surface_min)}"

    depts = set(departements)
    results: list[dict] = []
    seen: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"])
        try:
            for num in range(1, MAX_PAGES + 1):
                url = path if num == 1 else f"{path}/page:{num}"
                # Contexte NEUF par page : le challenge Cloudflare revient à chaque
                # navigation d'une même session et n'y est plus résolu — alors qu'un
                # contexte vierge le passe en quelques secondes.
                context = await browser.new_context(locale="fr-FR", user_agent=UA)
                page = await context.new_page()
                cards, soup = [], None
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    for _ in range(15):   # « Just a moment... » / « Un instant… »
                        title = (await page.title()).lower()
                        if "moment" not in title and "instant" not in title:
                            break
                        await asyncio.sleep(2)
                    else:
                        print("[Leggett] Challenge Cloudflare non résolu — abandon")
                        break
                    try:
                        await page.wait_for_selector("div.result-item", timeout=20000)
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                    soup = BeautifulSoup(await page.content(), "html.parser")
                    cards = soup.select("div.result-item")
                finally:
                    await context.close()
                added = 0
                for card in cards:
                    try:
                        b = _parse_card(card, depts)
                    except Exception:
                        continue
                    if not b or b["id_annonce"] in seen:
                        continue
                    if prix_max and b["prix"] and b["prix"] > prix_max:
                        continue
                    if prix_min and b["prix"] and b["prix"] < prix_min:
                        continue
                    if surface_min and b.get("surface") and b["surface"] < surface_min:
                        continue
                    seen.add(b["id_annonce"])
                    results.append(b)
                    added += 1
                print(f"[Leggett] page {num}: {added} biens")
                if not cards or f"page:{num + 1}" not in str(soup):
                    break
                await asyncio.sleep(1.5)
        except Exception as e:
            print(f"[Leggett] Erreur: {e}")
        finally:
            await browser.close()

    print(f"[Leggett] total: {len(results)} biens")
    return results


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Leggett")

"""
scrapers/figaro_immo.py — Figaro Immobilier (HTML Playwright)
Méthode : Playwright headless, parsing HTML BeautifulSoup
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

BASE_URL = "https://immobilier.lefigaro.fr"

DEPT_SLUGS = {
    "72": "sarthe",
    "45": "loiret",
    "61": "orne",
    "53": "mayenne",
    "36": "indre",
    "18": "cher",
    "28": "eure-et-loir",
    "41": "loir-et-cher",
    "37": "indre-et-loire",
    "49": "maine-et-loire",
    "44": "loire-atlantique",
    "85": "vendee",
    "86": "vienne",
    "79": "deux-sevres",
    "87": "haute-vienne",
    "23": "creuse",
    "03": "allier",
    "63": "puy-de-dome",
    "15": "cantal",
    "43": "haute-loire",
}

MAX_PAGES = 5  # 40 results/page → 200 max per dept


async def search(criteres: dict) -> list[dict]:
    departements = criteres.get("departements", [])
    prix_max = criteres.get("prix_max", 600000)
    surface_min = criteres.get("surface_min", 80)

    results = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="fr-FR",
        )

        for dept in departements:
            dept_str = str(dept).zfill(2)
            slug = DEPT_SLUGS.get(dept_str)
            if not slug:
                print(f"[FigaroImmo] Dept {dept}: slug inconnu, ignoré")
                continue

            try:
                biens = await _fetch_dept(context, dept_str, slug, prix_max, surface_min)
                results.extend(biens)
                print(f"[FigaroImmo] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[FigaroImmo] Erreur dept {dept}: {e}")

        await browser.close()

    return results


async def _fetch_dept(context, dept: str, slug: str, prix_max: int, surface_min: int) -> list[dict]:
    results = []

    for page_num in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/annonces/immobilier-vente-maison-{slug}.html"
        if page_num > 1:
            url += f"?page={page_num}"

        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await asyncio.sleep(1)
            html = await page.content()
        finally:
            await page.close()

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("article.classified-card")
        if not cards:
            break

        page_results = []
        for card in cards:
            bien = _parse_card(card, dept, prix_max, surface_min)
            if bien:
                page_results.append(bien)

        results.extend(page_results)

        # Stop if no more pages (fewer than 35 cards typically means last page)
        if len(cards) < 35:
            break

    return results


def _parse_card(card, dept: str, prix_max: int, surface_min: int) -> dict | None:
    try:
        # Link and ID
        link_el = card.select_one("a.content__link, a[href*='/annonces/annonce-']")
        if not link_el:
            return None
        href = link_el.get("href", "")
        if not href:
            return None
        url = href if href.startswith("http") else f"{BASE_URL}{href}"

        # Extract annonce ID from URL
        id_match = re.search(r"annonce-(\d+)", href)
        annonce_id = id_match.group(1) if id_match else re.sub(r"[^a-z0-9]", "", href)[-12:]

        # All listing text is concatenated in the link element
        text = link_el.get_text(" ", strip=True)

        # Prix — format: "392 700 €" or "392700€"
        prix_match = re.search(r"([\d\s]+)\s*€", text)
        prix = None
        if prix_match:
            prix = float(re.sub(r"\s", "", prix_match.group(1)))
            if prix > prix_max:
                return None

        # Surface — format: "180 m²"
        surf_match = re.search(r"(\d+(?:[,.]\d+)?)\s*m²", text)
        surface = None
        if surf_match:
            surface = float(surf_match.group(1).replace(",", "."))
            if surface < surface_min:
                return None

        # Pieces
        pieces_match = re.search(r"(\d+)\s*pi[èe]ces?", text, re.IGNORECASE)
        pieces = int(pieces_match.group(1)) if pieces_match else None

        # Chambres
        chambres_match = re.search(r"(\d+)\s*chambres?", text, re.IGNORECASE)
        chambres = int(chambres_match.group(1)) if chambres_match else None

        # Ville et code postal — format: "Change (72)" or "Le Mans (72000)"
        ville_match = re.search(r"([A-ZÀ-Ÿa-zà-ÿ][A-ZÀ-Ÿa-zà-ÿ\s\-\']+)\s*\((\d{5}|\d{2})\)", text)
        ville = ""
        code_postal = ""
        if ville_match:
            ville = ville_match.group(1).strip()
            cp_or_dept = ville_match.group(2)
            code_postal = cp_or_dept if len(cp_or_dept) == 5 else ""

        # Photos
        photos = []
        gallery = card.select_one("div.classified-card-gallery, div[class*='gallery']")
        if gallery:
            for img in gallery.select("img[src], img[data-src]"):
                src = img.get("src") or img.get("data-src", "")
                if src and src.startswith("http"):
                    photos.append(src)
                    break

        titre = f"Maison {pieces} p. {surface}m² {ville} ({dept})"

        return {
            "source": "figaro_immo",
            "url": url,
            "id_annonce": annonce_id,
            "titre": titre,
            "type_bien": "maison",
            "description": text[:1200],
            "departement": dept,
            "ville": ville,
            "code_postal": code_postal,
            "surface": surface,
            "surface_terrain": None,
            "pieces": pieces,
            "chambres": chambres,
            "prix": prix,
            "photos": photos,
            "dpe": None,
            "agence": None,
            "date_publication": None,
        }
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria
    criteres = load_criteria()
    biens = asyncio.run(search({
        "departements": criteres.departements[:2],
        "prix_max": criteres.prix_max,
        "surface_min": criteres.surface_min,
    }))
    print(f"\nTotal FigaroImmo: {len(biens)} annonces")
    for b in biens[:5]:
        print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

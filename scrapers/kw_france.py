"""
scrapers/kw_france.py — Keller Williams France
Méthode : Playwright — React CSR (le HTML initial est vide, le JS charge les annonces)
URL : /vente/maison?departement={code}  (une page nationale, filtrée par département)
Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re
import json
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

BASE = "https://www.kwfrance.com"

MAX_PAGES = 5


def _re_float(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text.replace("\xa0", " "))
    if not m:
        return None
    try:
        return float(m.group(1).replace(" ", "").replace(",", "."))
    except Exception:
        return None


def _re_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _re_str(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).upper() if m else None


def _parse_html(html: str, dept: str) -> list[dict]:
    """Parse le HTML rendu côté client par React."""
    soup = BeautifulSoup(html, "html.parser")

    # KW France utilise probablement des divs avec classes utilitaires (Tailwind ou similaire)
    # On essaie plusieurs sélecteurs
    cards = (
        soup.select("article")
        or soup.select("div[class*='PropertyCard']")
        or soup.select("div[class*='property-card']")
        or soup.select("div[class*='ListingCard']")
        or soup.select("div[class*='listing-card']")
        or soup.select("div[class*='card'][class*='property']")
        or soup.select("li[class*='property']")
        or soup.select("li[class*='listing']")
    )

    # Si toujours rien, cherche des liens qui ressemblent à des annonces
    if not cards:
        cards = [a.parent for a in soup.select("a[href*='/vente/maison/']") if a.parent]

    seen: set[str] = set()
    results = []
    for card in cards:
        try:
            b = _parse_card(card, dept)
            if b and b["url"] not in seen:
                seen.add(b["url"])
                results.append(b)
        except Exception:
            continue
    return results


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a[href]") if card.name != "a" else card
    if not link:
        return None
    href = link.get("href", "")
    if not href or href in ("#", "/"):
        return None
    url = href if href.startswith("http") else BASE + href

    id_m = re.search(r"/(\d{4,})", href)
    ad_id = id_m.group(1) if id_m else href.rstrip("/").split("/")[-1]

    text = card.get_text(" ", strip=True).replace("\xa0", " ")
    prix = _re_float(r"([\d][\d\s]*\d)\s*€", text)
    if not prix or prix < 10_000:
        return None

    surface = _re_float(r"(\d+(?:[.,]\d+)?)\s*m²", text)
    pieces = _re_int(r"(\d+)\s*pièces?", text)
    chambres = _re_int(r"(\d+)\s*ch(?:ambres?)?", text)
    dpe = _re_str(r"\bDPE\s*:?\s*([A-G])\b", text)

    city_m = re.search(r"([A-ZÀ-Ÿa-zà-ÿ][^(]{2,30})\s*\((\d{5})\)", text)
    ville = city_m.group(1).strip() if city_m else ""
    cp = city_m.group(2) if city_m else ""
    if cp and not cp.startswith(dept):
        return None

    title_el = card.select_one("h2, h3, h4, [class*='title'], [class*='Title']")
    titre = (title_el.get_text(strip=True) if title_el else f"Maison {ville}")[:150]

    photos = []
    for img in card.select("img"):
        for attr in ("src", "data-src", "data-lazy"):
            src = img.get(attr, "")
            if src and src.startswith("http"):
                photos.append(src)
                break
    photos = list(dict.fromkeys(photos))[:8]

    return {
        "source": "kw_france",
        "url": url,
        "id_annonce": str(ad_id),
        "titre": titre,
        "type_bien": "maison",
        "description": text[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Keller Williams",
    }


async def _scrape_dept(context, dept: str, prix_min: int, prix_max: int, surface_min: int) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page_num in range(1, MAX_PAGES + 1):
        url = f"{BASE}/vente/maison?departement={dept}"
        if page_num > 1:
            url += f"&page={page_num}"

        pw_page = await context.new_page()
        try:
            await pw_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # React CSR : attendre que les annonces se chargent via API
            try:
                await pw_page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(2)
            html = await pw_page.content()
        except Exception as e:
            print(f"[KWFrance] ERR dept={dept} page={page_num}: {e}")
            await pw_page.close()
            break
        finally:
            try:
                await pw_page.close()
            except Exception:
                pass

        parsed = _parse_html(html, dept)
        if not parsed:
            # Tente de détecter si la page indique "0 résultats"
            if "0 annonce" in html.lower() or "aucune annonce" in html.lower():
                print(f"[KWFrance] dept={dept} — 0 résultats")
            else:
                print(f"[KWFrance] dept={dept} page={page_num} — aucun card détecté (CSR peut-être incomplet)")
            break

        added = 0
        for b in parsed:
            if b["id_annonce"] in seen_ids:
                continue
            if prix_max and b.get("prix") and b["prix"] > prix_max:
                continue
            if prix_min and b.get("prix") and b["prix"] < prix_min:
                continue
            if surface_min and b.get("surface") and b["surface"] < surface_min:
                continue
            seen_ids.add(b["id_annonce"])
            biens.append(b)
            added += 1

        print(f"[KWFrance] dept={dept} page={page_num} → {added} biens")
        if added == 0:
            break

    return biens


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max    = criteres.get("prix_max", 600_000)
    prix_min    = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 80)

    results: list[dict] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="fr-FR",
        )
        for dept in departements:
            try:
                biens = await _scrape_dept(context, dept, prix_min, prix_max, surface_min)
                results.extend(biens)
                print(f"[KWFrance] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[KWFrance] Erreur dept {dept}: {e}")
        await browser.close()

    print(f"[KWFrance] total: {len(results)} biens")
    return results


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria
    criteres = load_criteria()

    async def _test():
        result = await search({
            "departements": [72, 53],
            "prix_max": criteres.prix_max,
            "prix_min": criteres.prix_min,
            "surface_min": criteres.surface_min,
        })
        print(f"\nTotal: {len(result)} annonces")
        for b in result[:5]:
            print(f"  {b['titre'][:70]} — {b['prix']}€ — {b['surface']}m² — {b['ville']}")

    asyncio.run(_test())

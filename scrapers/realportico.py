"""
scrapers/realportico.py — RealPortico (propriétés historiques : châteaux, manoirs…)
Méthode : scrape_simple (httpx) — SSR JSF (javax.faces).

Re-probé le 2026-07-02 : contrairement à la note historique de sources.yaml
(« aucune carte en httpx »), les pages département
/proprietes-historiques/vente/france/{region}/{dept} sont désormais rendues
serveur : cartes div.card.card-plain avec prix (« 840 000  EUR »), surfaces
utile/terrain et lien /annonce/{slug}/{id}. Inventaire de niche (0-2 biens par
département cible en 2026-07), pas de pagination au niveau département.
Interface : async def search(criteres: dict) -> list[dict]
"""
import re

from scrapers._base import parse_price_digits, run_dept_search

BASE = "https://www.realportico.fr"

# Code département → chemin region/departement du site.
DEPT_PATHS: dict[str, str] = {
    "72": "pays-de-la-loire/sarthe",
    "49": "pays-de-la-loire/maine-et-loire",
    "53": "pays-de-la-loire/mayenne",
    "28": "centre-val-de-loire/eure-et-loir",
    "45": "centre-val-de-loire/loiret",
    "37": "centre-val-de-loire/indre-et-loire",
    "36": "centre-val-de-loire/indre",
    "18": "centre-val-de-loire/cher",
    "41": "centre-val-de-loire/loir-et-cher",
    "89": "bourgogne-franche-comte/yonne",
    "58": "bourgogne-franche-comte/nievre",
}

_TYPE_VILLE = re.compile(r"^\s*(.+?)\s+à\s+vendre\s+(.+?)(?:,|$)", re.IGNORECASE)


def _page_url(dept: str, slug: str, page: int) -> str:
    return f"{BASE}/proprietes-historiques/vente/france/{slug}"


def _li_surface(card, label: str) -> float | None:
    for li in card.select("li"):
        if label in li.get_text(" ", strip=True):
            span = li.select_one("span[data-unit]")
            if span:
                digits = re.sub(r"[^\d]", "", span.get_text(strip=True))
                return float(digits) if digits else None
    return None


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a.viewDetail[href*='/annonce/']") or card.select_one("a[href*='/annonce/']")
    if not link:
        return None
    href = link.get("href", "")
    url = href if href.startswith("http") else BASE + href
    ad_id = link.get("data-expose") or (href.rstrip("/").split("/")[-1] if href else "")

    price_el = card.select_one("span.price.hidden-xs") or card.select_one("span.price")
    prix = parse_price_digits(price_el.get_text(strip=True)) if price_el else None
    if not prix or prix < 10_000:
        return None            # « prix sur demande » ou carte non exploitable

    title_el = card.select_one("span.fontBold.text-lead")
    titre = title_el.get_text(strip=True) if title_el else "Propriété historique"

    # Sous-titre : « Manoir à vendre Condéon, Nouvelle-Aquitaine »
    type_bien, ville = "maison", ""
    sub_el = card.select_one("div.text-preview")
    if sub_el:
        m = _TYPE_VILLE.search(sub_el.get_text(" ", strip=True))
        if m:
            type_bien = m.group(1).strip().lower() or "maison"
            ville = m.group(2).strip()

    cp_m = re.search(r"/annonce/(\d{5})-", href)
    cp = cp_m.group(1) if cp_m else ""

    desc_el = card.select_one("div.propertyDetails div.margin-5")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    photos = []
    img = card.select_one("img.flex-img[src]")
    if img:
        src = img["src"]
        photos.append(src if src.startswith("http") else BASE + src)

    return {
        "source": "realportico",
        "url": url,
        "id_annonce": str(ad_id),
        "titre": titre[:150],
        "type_bien": type_bien[:40],
        "description": description[:2000],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": _li_surface(card, "Surface utile"),
        "surface_terrain": _li_surface(card, "Surface du terrain"),
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "",
    }


async def search(criteres: dict) -> list[dict]:
    return await run_dept_search(
        source="realportico",
        page_url=_page_url,
        card_selector="div.card.card-plain",
        parse_card=_parse_card,
        criteres=criteres,
        dept_slugs=DEPT_PATHS,
        max_pages=1,           # pas de pagination sur les pages département
        dept_sleep=2.0,
        label="RealPortico",
    )


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "RealPortico")

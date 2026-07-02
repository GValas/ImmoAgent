"""scrapers/goodshowcase.py — Goodshowcase (portail thématique maisons de caractère / longères)

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : endpoint paginé index.php
              /index.php?mod=search&url_transaction[]=acheter&url_bien[]=maison
                         &id_dept[]={NN}&motcle[]={theme}&ordre=ajout&page={N}
              → filtre département CÔTÉ SERVEUR via id_dept[]={NN} (vérifié : aucune fuite).
              Les pages statiques /acheter-maison-{dept-slug}-{theme}.html sont
              l'équivalent page 1 ; on utilise index.php pour paginer proprement.

Thèmes scrapés : "caractere" (maisons de caractère) et "longere" (longères).

Cartes : div.panel.panel-default contenant un `h3 a` (les panels sans h3 a sont
         des en-têtes/filtres et sont ignorés).
  - URL/titre : h3 a[href]  → /annonce-{slug}-{id}.html  (id numérique en fin de slug)
  - Loc       : p[title]    → "Gidy 45520 - 0"  → ville + CP
  - Prix      : span.label  → "446 250 €"
  - Infos     : div.infos kbd[title]  → "Surface : 208 m²", "7 piéces"(T7), "3 chambres"
  - Photos    : a[href^='/ph/'] (vignettes pleine résolution) + img.thumbnail
  - Description: p.text-justify (extrait) ; agence parfois en tête ("X vous propose…")

Migré sur scrapers/_base.py : la double boucle dept × thème ne rentre pas dans
`run_dept_search` → on passe par `run_dept_api` (fetch_dept = thèmes + pagination,
le socle applique client, garde-fou CP strict, dédup id et filtres prix/surface).

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import (
    DEFAULT_DEPT_SLUGS,
    get_with_retry,
    parse_price_digits,
    run_dept_api,
    standalone_main,
)

BASE_URL = "https://www.goodshowcase.com"
SEARCH_URL = f"{BASE_URL}/index.php"
MAX_PAGES = 8
PHOTOS_PER_CARD = 10

# Thèmes du portail (mot-clé serveur motcle[])
THEMES = ["caractere", "longere"]


async def search(criteres: dict) -> list[dict]:
    return await run_dept_api(
        source="goodshowcase",
        label="Goodshowcase",
        fetch_dept=_fetch_dept,
        criteres=criteres,
        dept_slugs=DEFAULT_DEPT_SLUGS,  # documentaire : le filtre réel passe par id_dept[]=NN
        dept_sleep=0.6,
    )


async def _fetch_dept(client, dept: str, slug: str | None) -> list[dict]:
    """Balaye les deux thèmes du portail pour un département (pagination ?page=N).
    Dédup id locale inter-thèmes (une annonce peut matcher les deux mots-clés)."""
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for theme in THEMES:
        params = {
            "mod": "search",
            "url_transaction[]": "acheter",
            "url_bien[]": "maison",
            "id_dept[]": dept,
            "motcle[]": theme,
            "ordre": "ajout",
        }
        for page in range(1, MAX_PAGES + 1):
            params["page"] = str(page)
            r = await get_with_retry(client, SEARCH_URL, params=params)
            if r is None or r.status_code != 200:
                break

            soup = BeautifulSoup(r.text, "html.parser")
            cards = [
                p for p in soup.select("div.panel.panel-default")
                if p.select_one("h3 a")
            ]
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                try:
                    bien = _parse_card(card, dept)
                except Exception:
                    continue
                if not bien or bien["id_annonce"] in seen_ids:
                    continue
                seen_ids.add(bien["id_annonce"])
                biens.append(bien)
                new_on_page += 1

            if new_on_page == 0:
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(0.6)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    h3a = card.select_one("h3 a")
    if not h3a:
        return None
    href = h3a.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce : id numérique en fin de slug /annonce-...-41746452.html
    m_id = re.search(r"-(\d+)\.html", href)
    id_annonce = m_id.group(1) if m_id else url

    # Localisation : "Gidy 45520 - 0" — CP OBLIGATOIRE (post-filtre dept strict)
    loc_el = card.select_one("p[title]")
    loc = loc_el.get("title") or loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)
    if not code_postal:
        return None

    # Prix
    price_el = card.select_one("span.label")
    prix = parse_price_digits(price_el.get_text(" ", strip=True) if price_el else "")

    # Infos (kbd) : surface, pièces (Tn), chambres
    surface = None
    pieces = None
    chambres = None
    for kbd in card.select("div.infos kbd"):
        title = (kbd.get("title") or "").strip()
        txt = kbd.get_text(" ", strip=True)
        if re.search(r"Surface", title, re.IGNORECASE):
            surface = _parse_surface(title) or _parse_surface(txt)
        elif re.search(r"pi[eé]ces", title, re.IGNORECASE):
            m = re.search(r"(\d+)", title)
            if m:
                pieces = int(m.group(1))
        elif re.search(r"chambres?", title, re.IGNORECASE):
            m = re.search(r"(\d+)", title)
            if m:
                chambres = int(m.group(1))

    # Type de bien : déduit du slug d'URL (longere / caractere / maison)
    type_bien = _type_from_href(href)

    # Titre : le h3 dit "Vente maison" → on enrichit avec ville + type
    raw_title = h3a.get_text(" ", strip=True)
    titre = f"{type_bien.title()} {ville}".strip()
    if raw_title and raw_title.lower() not in ("vente maison", "vente"):
        titre = raw_title

    # Description (extrait)
    desc_el = card.select_one("p.text-justify")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    # Agence : parfois en tête de description ("XXX Immobilier vous propose…")
    agence = None
    m_ag = re.match(r"([A-ZÀ-Ÿ][\w'\-\.]*(?:\s+[A-ZÀ-Ÿ&][\w'\-\.]*){0,3})\s+vous\s+propose",
                    description)
    if m_ag:
        agence = m_ag.group(1).strip()[:80]

    # Photos : anchors /ph/{dept}/{cp}/XXX.jpg (pleine résolution) + thumbnail
    photos = []
    for a in card.select("a[href]"):
        h = a.get("href", "")
        if re.match(r"^/ph/\d", h) and h.lower().endswith((".jpg", ".jpeg", ".png")):
            photos.append(BASE_URL + h)
    if not photos:
        img = card.select_one("img.thumbnail")
        if img and img.get("src"):
            src = img["src"]
            photos.append(src if src.startswith("http") else BASE_URL + src)
    # dédoublonne en conservant l'ordre
    seen = set()
    photos = [p for p in photos if not (p in seen or seen.add(p))][:PHOTOS_PER_CARD]

    return {
        "source": "goodshowcase",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": agence,
    }


# ── Helpers propres à Goodshowcase (formats non couverts par _base) ─────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Gidy 45520 - 0' → ('Gidy', '45520')"""
    cp = ""
    m_cp = re.search(r"\b(\d{5})\b", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\b\d{5}\b.*$", "", text).strip()
    return ville, cp


def _parse_surface(text: str) -> float | None:
    """'Surface : 208 m²' → 208.0 (bornes 8-5000, sans mot-clé 'hab')"""
    m = re.search(r"([\d\s\xa0]+)\s*m", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 5000:
                return f
        except ValueError:
            pass
    return None


def _type_from_href(href: str) -> str:
    h = href.lower()
    if "longere" in h or "longère" in h:
        return "longère"
    if "manoir" in h:
        return "manoir"
    if "ferme" in h:
        return "ferme"
    if "propriete" in h or "propriété" in h:
        return "propriété"
    if "chateau" in h or "château" in h:
        return "château"
    if "moulin" in h:
        return "moulin"
    if "caractere" in h or "caractère" in h:
        return "maison de caractère"
    return "maison"


if __name__ == "__main__":
    standalone_main(search, "Goodshowcase")

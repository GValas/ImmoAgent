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
  - Infos     : div.infos kbd[title]  → "Surface : 208 m²", "7 piéces"(T7), "3 chambres", "5 photos"
  - Photos    : a[href^='/ph/'] (vignettes pleine résolution) + img.thumbnail
  - Description: p.text-justify (extrait)
  - Agence    : mentionnée parfois dans l'extrait (non structurée) → None

Post-filtre dept STRICT sur code_postal[:2] malgré le filtre serveur.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.goodshowcase.com"
SEARCH_URL = f"{BASE_URL}/index.php"
MAX_PAGES = 8
PHOTOS_PER_CARD = 10

# Thèmes du portail (mot-clé serveur motcle[])
THEMES = ["caractere", "longere"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Code département → slug (documentaire ; le filtre réel passe par id_dept[]=NN)
DEPT_SLUGS: dict[str, str] = {
    "72": "sarthe",
    "28": "eure-et-loir",
    "45": "loiret",
    "89": "yonne",
    "49": "maine-et-loire",
    "37": "indre-et-loire",
    "36": "indre",
    "18": "cher",
    "58": "nievre",
    "41": "loir-et-cher",
    "53": "mayenne",
}


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for dept in departements:
            if dept not in DEPT_SLUGS:
                continue
            seen_ids: set[str] = set()
            dept_count = 0
            for theme in THEMES:
                try:
                    biens = await _scrape_dept_theme(
                        client, dept, theme, prix_max, prix_min,
                        surface_min, seen_ids,
                    )
                    results.extend(biens)
                    dept_count += len(biens)
                except Exception as e:
                    print(f"[Goodshowcase] Erreur dept {dept} / {theme}: {e}")
                await asyncio.sleep(0.6)
            print(f"[Goodshowcase] Dept {dept}: {dept_count} annonces")

    return results


async def _scrape_dept_theme(
    client: httpx.AsyncClient,
    dept: str,
    theme: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
    seen_ids: set[str],
) -> list[dict]:
    biens: list[dict] = []

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
        r = await client.get(SEARCH_URL, params=params)
        if r.status_code != 200:
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
            if not bien:
                continue

            # Post-filtre dept STRICT (0 fuite)
            if not bien["code_postal"] or bien["code_postal"][:2] != dept:
                continue

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
            biens.append(bien)
            new_on_page += 1

        if new_on_page == 0:
            break

        await asyncio.sleep(0.5)

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

    # Localisation : "Gidy 45520 - 0"
    loc_el = card.select_one("p[title]")
    loc = loc_el.get("title") or loc_el.get_text(" ", strip=True) if loc_el else ""
    ville, code_postal = _parse_loc(loc)

    # Prix
    price_el = card.select_one("span.label")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Gidy 45520 - 0' → ('Gidy', '45520')"""
    cp = ""
    m_cp = re.search(r"\b(\d{5})\b", text)
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\b\d{5}\b.*$", "", text).strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'Surface : 208 m²' → 208.0"""
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
    print(f"\nTotal Goodshowcase: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p/{b.get('chambres') or '?'}ch"
            f" — {b['type_bien']} — {b['ville']}"
            f" — {len(b['photos'])} photos"
        )

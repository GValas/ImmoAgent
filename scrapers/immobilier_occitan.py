"""scrapers/immobilier_occitan.py — Immobilier Occitan (agence locale Lozère)

Méthode : scrape_simple (httpx) — SSR HTML (Symfony/Turbo, Apache)
Site    : https://www.immobilier-occitan.com
Segment : agence MONO-DÉPARTEMENT en Lozère (48). Ventes maisons / appartements /
          biens ruraux. Volume modeste (~20-25 annonces).

URL pattern (filtre département CÔTÉ SERVEUR par slug de chemin) :
    /ventes-biens-immobiliers-{slug}      ex: /ventes-biens-immobiliers-lozere
    → SEUL le slug "lozere" (48) renvoie 200 ; tout autre département → 404.
      L'agence ne couvre donc QUE le 48, hors de la zone cible actuelle
      (72/28/45/89...). Le scraper reste générique : si un département Occitanie
      devient cible, ajouter son slug dans DEPT_SLUGS.

Cartes (page liste) : article.swiper-slide > a.block[href="/propriete/{slug}"]
  - URL    : a.block[href]            → /propriete/{slug}
  - Image  : img[src]                 (media/cache/optimized_full/...)
  - Titre  : h1.font-semibold
  - Statut : div "Disponible" / "Vendu" (badge en haut-gauche) → on jette "Vendu"
  - Prix   : bloc .text-green-700      → "235 000,00 €"
  - Opts   : 3 <span> (chambres, salles de bain, surface m²) repérés par
             data-icon (double-bed / shower-head / triangle-ruler)
  ⚠ Le CODE POSTAL n'est PAS dans la carte → il faut ouvrir la page détail.

Page détail : <dl> ... <dt>Adresse</dt><dd>{rue} - {CP} {VILLE}</dd>
  → c'est de là qu'on tire code_postal / ville pour le post-filtre STRICT.

Stratégie filtre dept : slug serveur + post-filtre STRICT code_postal[:2] == dept
  (les CP des annonces vivent sur la page détail ; on les récupère pour chaque
  carte retenue). 0 fuite garantie.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.immobilier-occitan.com"
PHOTOS_PER_CARD = 10
MAX_DETAILS = 60  # garde-fou sur le nombre de pages détail ouvertes

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Code département → slug d'URL /ventes-biens-immobiliers-{slug}
# Seul "lozere" (48) renvoie des annonces (agence mono-département). Les autres
# slugs Occitanie sont prêts au cas où l'agence s'étendrait / la zone changerait.
DEPT_SLUGS: dict[str, str] = {
    "48": "lozere",
    "12": "aveyron",
    "30": "gard",
    "34": "herault",
    "46": "lot",
    "07": "ardeche",
    "15": "cantal",
    "43": "haute-loire",
}

# Types de bien conservés / exclus (déduits du titre)
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|long[èe]re|manoir|ch[âa]teau|mas|"
    r"grange|domaine|corps de ferme|maison de village|maisonnette|demeure",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|terrain|local|commerce|garage|parking|immeuble|"
    r"bureau|fonds|cave|box",
    re.IGNORECASE,
)


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
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                # Département hors couverture de l'agence (cas normal pour la
                # zone cible actuelle 72/28/45/89) → rien à scraper.
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, slug, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[ImmoOccitan] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[ImmoOccitan] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    slug: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    url = f"{BASE_URL}/ventes-biens-immobiliers-{slug}"
    r = await client.get(url)
    if r.status_code != 200:
        print(f"[ImmoOccitan] Dept {dept}: page {r.status_code} (non couvert)")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    cards = soup.select("article.swiper-slide")
    if not cards:
        return []

    biens: list[dict] = []
    seen: set[str] = set()
    details_done = 0

    for card in cards:
        if details_done >= MAX_DETAILS:
            break
        try:
            base = _parse_card(card)
        except Exception:
            continue
        if not base:
            continue
        if base["url"] in seen:
            continue
        seen.add(base["url"])

        # Le code postal n'est pas dans la carte → page détail obligatoire
        try:
            cp, ville, desc, dpe = await _fetch_detail(client, base["url"])
        except Exception:
            cp, ville, desc, dpe = "", "", "", None
        details_done += 1
        await asyncio.sleep(0.5)

        base["code_postal"] = cp
        if ville:
            base["ville"] = ville[:80]
        if desc:
            base["description"] = desc[:1200]
        base["dpe"] = dpe
        base["departement"] = dept

        # Post-filtre dept STRICT : 0 fuite hors-zone
        if not cp or cp[:2] != dept:
            continue

        p = base.get("prix") or 0
        s = base.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        biens.append(base)

    return biens


def _parse_card(card) -> dict | None:
    link = card.select_one("a.block[href]") or card.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href or "/propriete/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Statut : on jette les biens vendus / sous compromis
    statut = card.get_text(" ", strip=True).lower()
    if re.search(r"\bvendu\b|sous compromis|sous offre", statut):
        return None

    title_el = card.select_one("h1")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    # Type de bien (depuis le titre)
    if _EXCLUDE_TYPE.search(titre) and not _KEEP_TYPE.search(titre):
        return None
    if not _KEEP_TYPE.search(titre):
        return None
    type_bien = _type_from_title(titre)

    # Prix : bloc .text-green-700  →  "Prix FAI : 235 000,00 €"
    price_el = card.select_one(".text-green-700")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Options : <span> repérés par data-icon (chambres / sdb / surface)
    chambres = _icon_value(card, "double-bed")
    surface = _icon_value(card, "triangle-ruler")
    # (le data-icon shower-head = salles de bain, non mappé au modèle Bien)

    # Photos
    photos = []
    for img in card.select("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    # id_annonce : slug de l'URL
    id_annonce = href.rstrip("/").split("/")[-1] or url

    return {
        "source": "immobilier_occitan",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": None,
        "ville": "",
        "code_postal": "",
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Immobilier Occitan",
    }


async def _fetch_detail(
    client: httpx.AsyncClient, url: str
) -> tuple[str, str, str, str | None]:
    """Récupère code_postal, ville, description, dpe sur la page détail.

    Adresse dans un <dl> : <dt>Adresse</dt><dd>{rue} - {CP} {VILLE}</dd>.
    """
    r = await client.get(url)
    if r.status_code != 200:
        return "", "", "", None
    soup = BeautifulSoup(r.text, "html.parser")

    cp, ville = "", ""
    # 1) Bloc Adresse (dt/dd)
    for dt in soup.find_all("dt"):
        if "adresse" in dt.get_text(strip=True).lower():
            dd = dt.find_next_sibling("dd") or dt.find_next("dd")
            if dd:
                cp, ville = _parse_cp_ville(dd.get_text(" ", strip=True))
            break
    # 2) Repli : premier "CP VILLE" dans le corps de l'annonce
    if not cp:
        main = soup.find("main") or soup
        cp, ville = _parse_cp_ville(main.get_text(" ", strip=True))

    # Description
    desc = ""
    desc_el = soup.select_one("[class*='prose'], article p, .description")
    if desc_el:
        desc = desc_el.get_text(" ", strip=True)
    if not desc and soup.h1:
        desc = soup.h1.get_text(" ", strip=True)

    # DPE (lettre A-G) si présent dans un libellé DPE
    dpe = None
    m_dpe = re.search(
        r"(?:DPE|classe[^A-G]{0,20})\b[:\s]*([A-G])\b", soup.get_text(" ", strip=True)
    )
    if m_dpe:
        dpe = m_dpe.group(1)

    return cp, ville, desc, dpe


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_cp_ville(text: str) -> tuple[str, str]:
    """'route de vals  - 48230 CHANAC' → ('48230', 'Chanac')"""
    m = re.search(r"\b(\d{5})\s+([A-Za-zÀ-ÿ'’\-\.\s]+?)(?:\s{2,}|$|\.|,)", text)
    if not m:
        m = re.search(r"\b(\d{5})\b", text)
        return (m.group(1), "") if m else ("", "")
    cp = m.group(1)
    ville = re.sub(r"\s+", " ", m.group(2)).strip().title()
    return cp, ville


def _type_from_title(titre: str) -> str:
    t = titre.lower()
    for kw in (
        "château", "manoir", "mas", "ferme", "longère", "longere", "grange",
        "domaine", "propriété", "propriete", "villa", "maison de village",
        "maisonnette", "maison",
    ):
        if kw in t:
            return kw
    return "maison"


def _parse_price(text: str) -> float | None:
    if not text:
        return None
    # "Prix FAI : 235 000,00 €" → 235000.0
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r",\d{2}$", "", cleaned)  # retire les centimes ,00
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _icon_value(card, icon_kw: str) -> int | float | None:
    """Valeur numérique du <span> dont l'icône matche icon_kw (m² ou compteur)."""
    icon = card.find("span", attrs={"data-icon": re.compile(icon_kw)})
    if not icon:
        return None
    container = icon.parent
    txt = container.get_text(" ", strip=True) if container else ""
    m = re.search(r"(\d[\d\s\xa0]*)", txt)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        return float(val) if "ruler" in icon_kw else int(val)
    except ValueError:
        return None


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
    print(f"\nTotal Immobilier Occitan: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

"""scrapers/bellespierres.py — BellesPierres (agrégateur de biens de caractère / prestige)

Méthode : scrape_simple (httpx) — SSR HTML (Vue.js mais contenu rendu côté serveur).
Agrégateur de demeures de caractère, manoirs, châteaux et propriétés (souvent
équestres : "Domaine équestre", "Propriété Équestre") issus d'agences partenaires
(Barnes, Junot, Kretz, Cabinet Le Nail, Sotheby's Val de Loire, Equestrian
Immobilier, Aurélie Durier…). Couvre la totalité des 11 départements cibles.

URL pattern (chemin EN — le seul qui filtre réellement par département ; le chemin
/fr/vente/bien/{slug}-{NN}/ ignore le slug et renvoie un mix national → NE PAS
utiliser) :
    /en/sale/luxury-real-estate/{slug}-{NN}/[?page=P]
    ex: /en/sale/luxury-real-estate/indre-et-loire-37/

Accès : le site renvoie 403 "Accès restreint" sur un hit direct. Contournement
fiable et léger : GET de la page d'accueil d'abord (pose un cookie de session),
puis les pages liste avec un Referer interne. Pas de Cloudflare/DataDome
(aucun cf-ray, pas de challenge JS).

Cartes : article[data-t="carte-annonce"] (classe .offer-card)
  - URL   : a.offer-card__offer-link[href]
            → /en/sale/{type}/{ville-CODEPOSTAL}/exceptionnal-property-{id}/
            le CODE POSTAL est dans le slug → filtre département STRICT côté client.
  - Prix  : .offer-card__price            → "€577,500"
  - Titre : .offer-card__title            (texte FR)
  - Loc   : .offer-card__location         → "Ville - Département"
  - Carac.: .offer-card__caracteristics__item  → "198.93m²", "10 rooms", "6 beds"
  - Agence: .offer-card__agency__name
  - Photos: img.offer-card__slider__pic[src]

Type de bien : déduit du segment d'URL (luxury-house, luxury-properties, manor,
               castle, villa). Appartements/terrains exclus par prudence.

Filtre département : le slug {NN} filtre côté serveur, MAIS quelques biens
limitrophes fuient (vu : 28→1, 18→2, 41→1 fuites). Le post-filtre STRICT sur
le code postal extrait de l'URL (code_postal[:2] == dept) ramène la fuite à 0.

Particularité : portail de prestige → prix souvent élevés (beaucoup > prix_max) ;
le filtre prix s'applique normalement, le parsing reste valide quel que soit le stock.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.bellespierres.com"
MAX_PAGES = 6
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "upgrade-insecure-requests": "1",
}

# Code département → slug URL bellespierres.com (chemin EN qui filtre vraiment)
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

# Types de bien (segment d'URL) à conserver
_KEEP_TYPE = re.compile(
    r"house|maison|propert|propriete|propriété|villa|castle|chateau|château|"
    r"manor|manoir|farm|ferme|mill|moulin|estate|domaine|mas",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|apartment|terrain|land|local|commerce|garage|parking|"
    r"immeuble|building|bureau|office|fonds",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # Warm-up : pose le cookie de session pour éviter le 403 "Accès restreint"
        try:
            await client.get(BASE_URL + "/")
        except Exception as e:
            print(f"[BellesPierres] Warm-up échoué ({e}) — on tente quand même")

        for dept in departements:
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, slug, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[BellesPierres] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[BellesPierres] Erreur dept {dept}: {e}")
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
    biens: list[dict] = []
    seen_ids: set[str] = set()
    base = f"{BASE_URL}/en/sale/luxury-real-estate/{slug}-{dept}/"

    for page in range(1, MAX_PAGES + 1):
        url = base + (f"?page={page}" if page > 1 else "")
        r = await client.get(url, headers={"Referer": BASE_URL + "/"})
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select(
            "article[data-t=carte-annonce]"
        )
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

            # Post-filtre STRICT : seul le département cible (le slug serveur fuit un peu)
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

        # Plus aucun bien nouveau retenu sur la page → fin
        if new_on_page == 0 and page > 1:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    link = card.select_one("a.offer-card__offer-link[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # /en/sale/{type}/{ville-CODEPOSTAL}/exceptionnal-property-{id}/
    parts = [p for p in href.split("/") if p]
    type_seg = parts[2] if len(parts) > 2 else ""
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg.replace("luxury-", "").replace("-", " ").strip() or "maison"

    # Code postal depuis le slug ville (ex: fondettes-37230)
    code_postal = ""
    loc_seg = parts[3] if len(parts) > 3 else ""
    m_cp = re.search(r"-(\d{5})$", loc_seg)
    if m_cp:
        code_postal = m_cp.group(1)

    # id_annonce depuis le slug final (exceptionnal-property-1419968)
    id_annonce = ""
    if parts:
        m_id = re.search(r"(\d{5,})", parts[-1])
        if m_id:
            id_annonce = m_id.group(1)
    if not id_annonce:
        id_annonce = url

    # Localisation affichée : "Ville - Département"
    loc_el = card.select_one(".offer-card__location")
    loc_txt = loc_el.get_text(" ", strip=True) if loc_el else ""
    ville = loc_txt.split(" - ")[0].strip() if loc_txt else ""
    if not ville and loc_seg:
        ville = re.sub(r"-\d{5}$", "", loc_seg).replace("-", " ").title()

    # Titre
    title_el = card.select_one(".offer-card__title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Prix : "€577,500"
    price_el = card.select_one(".offer-card__price")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Caractéristiques : "198.93m²", "10 rooms", "6 beds"
    surface = None
    pieces = None
    chambres = None
    for item in card.select(".offer-card__caracteristics__item"):
        t = item.get_text(" ", strip=True)
        if surface is None and re.search(r"m²", t):
            surface = _parse_surface(t)
        elif re.search(r"room|pi[eè]ce", t, re.IGNORECASE):
            pieces = _parse_first_int(t)
        elif re.search(r"bed|chambre", t, re.IGNORECASE):
            chambres = _parse_first_int(t)

    # Agence partenaire
    ag_el = card.select_one(".offer-card__agency__name")
    agence = ag_el.get_text(" ", strip=True) if ag_el else "BellesPierres"

    # Photos
    photos: list[str] = []
    for img in card.select("img.offer-card__slider__pic"):
        src = img.get("src") or ""
        if src and not src.startswith("data:") and src not in photos:
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "bellespierres",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
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

def _parse_price(text: str) -> float | None:
    """'€577,500' / '1 640 000 €' → 577500.0 / 1640000.0"""
    cleaned = re.sub(r"[€\s\xa0,]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'198.93m²' → 198.93"""
    m = re.search(r"([\d\s\xa0]+(?:[.,]\d+)?)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
        try:
            f = float(val)
            if 8 <= f <= 5000:
                return f
        except ValueError:
            pass
    return None


def _parse_first_int(text: str) -> int | None:
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
    print(f"\nTotal BellesPierres: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p/{b.get('chambres') or '?'}ch"
            f" — {b['type_bien']} — {b['ville']} — {b['agence']}"
        )

"""scrapers/carre_mans.py — Carré Mans Immobilier (agence indépendante Le Mans / Sarthe)

Méthode : scrape_simple (httpx) — SSR HTML (CMS « Kocka »)
Site mono-agence : tout l'inventaire de vente est sur une seule page
  /vente-biens-immobiliers-appartements-maisons.html (pas de pagination,
  ~20-25 biens). Pas de slug/param par département : l'agence n'opère qu'en
  Sarthe (72) → on scrape la page de vente puis on POST-FILTRE strictement sur
  le code postal récupéré en page détail (CP[:2] == dept).

Cartes liste : <article> contenant
  - URL détail : a[href="vente-{type}-sur-{ville-slug}-{ref}-companyXXXXXvsr.html"]
  - Type       : déduit du slug d'URL (maison / appartement / terrain / immeuble)
  - Prix       : .prix_ok span  → "161 000 € FAI"
  - Titre/loc  : .titre h3 a[title]  → "Vente Maison de 240.00m², Parigné-l'Évêque"
                 (la ville est le dernier segment après la virgule)
  - Surface    : .infos  → "240.00 m 2 …"
  - Chambres   : .infos  → "N chambres"
  - Photo      : figure img[data-src]  (lazy)

Le code postal n'est PAS présent en liste → on l'extrait en page détail
(texte "Ville (CP)") en l'appariant à la ville de la carte. La description,
les photos additionnelles et la surface de terrain (le cas échéant) sont aussi
récupérées en détail. Comme l'agence est 100 % Sarthe, seul le dept 72 ressort ;
les autres départements demandés donnent 0 bien (aucune fuite).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import unicodedata

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.carre-mans.fr"
LISTING_URL = f"{BASE_URL}/vente-biens-immobiliers-appartements-maisons.html"
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien conservés (segment d'URL) : maisons / propriétés
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commercial|commerce|garage|parking|immeuble|"
    r"bureau|fonds|entrepot|entrepôt",
    re.IGNORECASE,
)

_DETAIL_RE = re.compile(
    r"vente-([a-zàâäéèêëïîôöùûüç]+)-sur-(.+?)-([a-z]+\d+)-company\d+vsr\.html",
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
        try:
            r = await client.get(LISTING_URL)
        except Exception as e:
            print(f"[CarréMans] Erreur listing: {e}")
            return results
        if r.status_code != 200:
            print(f"[CarréMans] Listing HTTP {r.status_code}")
            return results

        cards = BeautifulSoup(r.text, "html.parser").find_all("article")
        candidates: list[dict] = []
        seen: set[str] = set()
        for card in cards:
            try:
                base = _parse_card(card)
            except Exception:
                continue
            if not base:
                continue
            if base["id_annonce"] in seen:
                continue

            # Pré-filtres connus dès la liste (type / prix / surface habitable)
            p = base.get("prix") or 0
            s = base.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            seen.add(base["id_annonce"])
            candidates.append(base)

        print(f"[CarréMans] {len(candidates)} biens candidats (avant filtre dept)")

        # Enrichissement détail (CP, description, photos, terrain) sur les survivants
        for base in candidates:
            try:
                bien = await _enrich_detail(client, base)
            except Exception as e:
                print(f"[CarréMans] détail KO {base['url']}: {e}")
                bien = None
            if not bien:
                continue

            cp = bien.get("code_postal") or ""
            # Filtre département STRICT : CP[:2] doit être dans la zone cible
            if not cp or cp[:2] not in departements:
                continue
            bien["departement"] = cp[:2]
            results.append(bien)
            await asyncio.sleep(0.5)

    vus = sorted({b["code_postal"][:2] for b in results if b["code_postal"]})
    print(f"[CarréMans] {len(results)} biens retenus — depts {vus}")
    return results


def _parse_card(card) -> dict | None:
    link = card.find("a", href=_DETAIL_RE)
    if not link:
        return None
    href = link.get("href", "")
    m = _DETAIL_RE.search(href)
    if not m:
        return None
    type_seg, ville_slug, ref = m.group(1), m.group(2), m.group(3)

    # Filtre type (maisons / propriétés uniquement)
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg.lower()

    url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

    # Titre + ville (dernier segment après la virgule)
    h3 = card.select_one(".titre h3 a")
    titre = ""
    ville = ""
    if h3:
        titre = h3.get("title") or h3.get_text(" ", strip=True)
        titre = re.sub(r"<sup>.*?</sup>", "", titre)
        titre = re.sub(r"\s+", " ", titre).strip()
        if "," in titre:
            ville = titre.rsplit(",", 1)[-1].strip()
    if not ville:
        ville = ville_slug.replace("-", " ").strip().title()
    if not titre:
        titre = f"Vente {type_bien.title()} {ville}".strip()

    # Prix
    price_el = card.select_one(".prix_ok span") or card.select_one(".prix")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Surface habitable + chambres depuis .infos
    infos_el = card.select_one(".infos")
    infos_text = infos_el.get_text(" ", strip=True) if infos_el else ""
    surface = _parse_surface(infos_text)
    chambres = _parse_int(r"(\d+)\s*chambre", infos_text)

    # Photo principale (liste)
    photos = []
    fig_img = card.select_one("figure img")
    if fig_img:
        src = fig_img.get("data-src") or fig_img.get("src") or ""
        if src and not src.endswith("blank.png") and not src.startswith("data:"):
            photos.append(_abs_media(src))

    return {
        "source": "carre_mans",
        "url": url,
        "id_annonce": ref,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": "",
        "ville": ville[:80],
        "code_postal": "",
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Carré Mans Immobilier",
    }


async def _enrich_detail(client: httpx.AsyncClient, base: dict) -> dict | None:
    r = await client.get(base["url"])
    if r.status_code != 200:
        return None
    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    # Code postal : apparier "Ville (CP)" à la ville de la carte
    base["code_postal"] = _extract_cp(html, base["ville"])

    # Description
    desc = ""
    desc_el = (
        soup.select_one(".description")
        or soup.select_one("#description")
        or soup.find("meta", attrs={"name": "description"})
    )
    if desc_el is not None:
        if desc_el.name == "meta":
            desc = desc_el.get("content", "")
        else:
            desc = desc_el.get_text(" ", strip=True)
    base["description"] = re.sub(r"\s+", " ", desc).strip()[:1200]

    # Pièces / terrain depuis le texte de la page
    full = soup.get_text(" ", strip=True)
    if base.get("pieces") is None:
        base["pieces"] = _parse_int(r"(\d+)\s*pi[eè]ces?", full)
    if base.get("surface_terrain") is None:
        base["surface_terrain"] = _parse_terrain(full)
    # DPE non extrait : la page n'affiche que l'échelle A-G en texte (la vraie
    # classe est dans une image SVG), parser le texte donnerait une fausse valeur.

    # Photos additionnelles — la galerie détail expose les images du bien en
    # src direct (filename contenant la référence, ex. ..._vm404_9_original.jpg).
    ref = (base.get("id_annonce") or "").lower()
    photos = list(base.get("photos") or [])
    for img in soup.select("img"):
        src = img.get("data-src") or img.get("data-original") or img.get("src") or ""
        if not src or src.endswith("blank.png") or src.startswith("data:"):
            continue
        if "media/" not in src or "/logo/" in src:
            continue
        # Ne garder que les photos du bien (filename portant la référence)
        if ref and ref not in src.lower():
            continue
        u = _abs_media(src)
        if u not in photos:
            photos.append(u)
        if len(photos) >= PHOTOS_PER_CARD:
            break
    base["photos"] = photos[:PHOTOS_PER_CARD]

    return base


# ── Helpers ──────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _extract_cp(html: str, ville: str) -> str:
    """Apparie 'Ville (CP)' au nom de ville de la carte (évite le CP de l'agence)."""
    pairs = re.findall(r"([A-Za-zÀ-ÿ'\- ]{2,40})\s*\((\d{5})\)", html)
    nv = _norm(ville)
    for nm, cp in pairs:
        nm_n = _norm(nm)
        if nv and (nm_n == nv or nv in nm_n or nm_n.endswith(nv)):
            return cp
    # Repli : premier CP rencontré (page mono-bien Sarthe)
    return pairs[0][1] if pairs else ""


def _abs_media(src: str) -> str:
    if src.startswith("http"):
        return src
    if src.startswith("//"):
        return "https:" + src
    return f"{BASE_URL}/{src.lstrip('/')}"


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text.split("€")[0]) if "€" in text else re.sub(r"[^\d]", "", text)
    try:
        v = float(cleaned) if cleaned else None
        return v if v and v > 1000 else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'240.00 m 2 5 chambres' → 240.0"""
    m = re.search(r"(\d+[.,]?\d*)\s*m", text)
    if m:
        try:
            v = float(m.group(1).replace(",", "."))
            if 5 <= v <= 5000:
                return v
        except ValueError:
            pass
    return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_terrain(text: str) -> float | None:
    m = re.search(r"terrain[^\d]{0,15}([\d\s\xa0]{1,9})\s*m", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            v = float(val)
            if v >= 10:
                return v
        except ValueError:
            pass
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
    print(f"\nTotal Carré Mans: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

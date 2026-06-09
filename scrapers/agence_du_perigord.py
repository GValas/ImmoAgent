"""scrapers/agence_du_perigord.py — Agence du Périgord (agence indépendante Dordogne)

Méthode : scrape_simple (httpx) — SSR HTML (template "Activimmo")
Site     : https://www.agenceduperigord.fr
Segment  : agence régionale indépendante du Périgord (Dordogne, 24) — maisons,
           propriétés de caractère, manoirs, demeures, domaines ruraux ; déborde
           sur quelques secteurs limitrophes (Lot 46, Corrèze 19, Lot-et-Garonne 47…).

Filtre département — stratégie :
  La liste `?action=list&ctypmandatmeta=v` n'expose PAS le code postal sur les
  cartes (juste un slug ville dans l'URL détail et un libellé "Région X").
  MAIS le formulaire de recherche porte un select `cregion` dont CHAQUE option a
  pour `value` le CODE POSTAL du secteur (ex: 24000 PERIGUEUX, 46000 CAHORS…).
  Le param `&cregion={CP}` filtre la liste CÔTÉ SERVEUR sur ce secteur.
  → On lit les options `cregion`, on retient celles dont `CP[:2]` ∈ départements
    cibles, et on interroge un secteur (= un CP connu) à la fois. Le code postal
    de chaque bien est donc connu de façon fiable = celui du secteur interrogé.
  → Post-filtre STRICT `CP[:2] == dept` malgré tout (0 fuite).

  Couverture réelle (secteurs cregion) : 24 (≈29), 46 (≈20), + 12/15/19/42/47/87.
  Sur les départements Val-de-Loire/Ouest (72/28/45/89/49/37…) : AUCUN secteur
  → 0 bien (normal, agence mono-Dordogne).

Cartes (liste) : div#postcontentback
  - URL    : a.linkdetails[href*="/bien/"]  → .../offre/{ville}/bien/{id}/{slug}.html
  - Titre  : h1 a.linkdetails
  - Chambres : span.chambre   (texte numérique, parfois vide)
  - SdB      : span.sdb
  - Terrain  : span.terrain   ("4.013m²" → point = séparateur de milliers → 4013)
  - Garage   : span.garage  / Piscine : span.piscine
  - Réf    : "Ref: XXXX" dans le header
  - Prix   : texte "318.000 €" (point = séparateur de milliers)
  - Desc   : <p> de la section
  - Photo  : img#postcontentimg[src]
  Surface habitable : absente de la liste → extraite du titre/description si présente.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.agenceduperigord.fr"
LIST_PATH = "/index.php?action=list&ctypmandatmeta=v"
MAX_PAGES = 6
PHOTOS_PER_CARD = 1  # la liste n'expose qu'une photo par carte

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien à conserver (titre / slug ville d'URL) — maisons & propriétés
_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|mas|g[iî]te|corps[- ]de[- ]ferme|repaire|b[aâ]tisse|"
    r"p[eé]rigourdine|caract[eè]re|maison de ma[iî]tre|grange",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"^\s*terrain|appartement|garage seul|parking|"
    r"local commercial|fonds de commerce|bureau[x]?",
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
        # 1) Récupérer la liste des secteurs (cregion) = code postal par option
        cp_by_dept = await _load_cregion_cps(client, departements)

        for dept in departements:
            cps = cp_by_dept.get(dept, [])
            if not cps:
                print(f"[AgenceDuPerigord] Dept {dept}: aucun secteur (hors zone agence)")
                continue
            dept_biens: list[dict] = []
            seen: set[str] = set()
            for cp in cps:
                try:
                    biens = await _scrape_secteur(
                        client, dept, cp, prix_max, prix_min, surface_min, seen
                    )
                    dept_biens.extend(biens)
                except Exception as e:
                    print(f"[AgenceDuPerigord] Erreur secteur {cp}: {e}")
                await asyncio.sleep(0.5)
            results.extend(dept_biens)
            print(f"[AgenceDuPerigord] Dept {dept}: {len(dept_biens)} annonces")

    return results


async def _load_cregion_cps(
    client: httpx.AsyncClient, departements: list[str]
) -> dict[str, list[str]]:
    """Lit le select #cregion et regroupe les codes postaux des secteurs par dept."""
    out: dict[str, list[str]] = {}
    if not departements:
        return out
    r = await client.get(BASE_URL + LIST_PATH)
    if r.status_code != 200:
        return out
    sel = BeautifulSoup(r.text, "html.parser").find("select", {"id": "cregion"})
    if not sel:
        return out
    for opt in sel.find_all("option"):
        val = (opt.get("value") or "").strip()
        if not re.fullmatch(r"\d{5}", val):
            continue  # ignore les valeurs obfusquées (tokens) et l'option vide
        dept = val[:2]
        if dept in departements:
            out.setdefault(dept, [])
            if val not in out[dept]:
                out[dept].append(val)
    return out


async def _scrape_secteur(
    client: httpx.AsyncClient,
    dept: str,
    cp: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
    seen: set[str],
) -> list[dict]:
    biens: list[dict] = []

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/index.php?page={page}&action=list&ctypmandatmeta=v&cregion={cp}"
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select("div#postcontentback")
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            try:
                bien = _parse_card(card, dept, cp)
            except Exception:
                continue
            if not bien:
                continue

            # Filtre département STRICT (le CP vient du secteur interrogé)
            if not bien["code_postal"] or bien["code_postal"][:2] != dept:
                continue

            aid = bien["id_annonce"]
            if aid in seen:
                continue

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            seen.add(aid)
            biens.append(bien)
            new_on_page += 1

        if new_on_page == 0:
            break
        await asyncio.sleep(0.4)

    return biens


def _parse_card(card, dept: str, cp: str) -> dict | None:
    link = card.select_one('a.linkdetails[href*="/bien/"]')
    if not link:
        return None
    href = link.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id numérique depuis l'URL : /bien/{id}/{slug}.html
    m_id = re.search(r"/bien/(\d+)/", href)
    id_num = m_id.group(1) if m_id else ""

    # ville (slug d'URL : /offre/{ville}/bien/...)
    m_ville = re.search(r"/offre/([^/]+)/bien/", href)
    ville_slug = m_ville.group(1) if m_ville else ""
    ville = ville_slug.replace("-", " ").strip().title()

    # Titre
    title_el = card.select_one("h1 a.linkdetails") or card.select_one("h1")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    full_text = card.get_text(" ", strip=True)

    # Référence (id_annonce de secours)
    m_ref = re.search(r"Ref:\s*([\w.\-]+)", full_text)
    ref = m_ref.group(1) if m_ref else ""
    id_annonce = id_num or ref or url

    # Filtrage type de bien (sur titre + slug ville d'URL)
    # Terrain nu / parking / commerce : exclusion ferme (titre prioritaire)
    if re.match(r"\s*terrain\b", titre, re.IGNORECASE):
        return None
    type_src = f"{titre} {ville_slug}"
    if _EXCLUDE_TYPE.search(type_src) and not _KEEP_TYPE.search(type_src):
        return None
    type_bien = _deduce_type(titre)

    # Description
    p_el = card.select_one("section.post-content p") or card.select_one("p")
    description = p_el.get_text(" ", strip=True) if p_el else ""

    # Prix : "318.000 €" → 318000 (point = séparateur de milliers)
    prix = _parse_price(full_text)

    # Chambres / terrain depuis les span dédiés
    chambres = _span_int(card, "span.chambre")
    surface_terrain = _span_surface(card, "span.terrain")

    # Surface habitable : pas dans la liste → tentative titre/description
    surface = _parse_surface_hab(titre) or _parse_surface_hab(description)

    # Photo principale
    photos = []
    img = card.select_one("img#postcontentimg")
    if img:
        src = img.get("src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            photos.append(src.split("?")[0])
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "agence_du_perigord",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Agence du Périgord",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _deduce_type(titre: str) -> str:
    t = (titre or "").lower()
    for kw, label in [
        ("manoir", "manoir"),
        ("château", "château"),
        ("chateau", "château"),
        ("domaine", "domaine"),
        ("moulin", "moulin"),
        ("ferme", "ferme"),
        ("longère", "longère"),
        ("longere", "longère"),
        ("maison de maître", "maison de maître"),
        ("propriété", "propriété"),
        ("propriete", "propriété"),
        ("demeure", "demeure"),
        ("gîte", "propriété"),
        ("gite", "propriété"),
        ("maison", "maison"),
        ("villa", "villa"),
    ]:
        if kw in t:
            return label
    return "maison"


def _parse_price(text: str) -> float | None:
    """'318.000 €' / '2.180.000 €' → 318000.0 / 2180000.0 (point = milliers)."""
    m = re.search(r"([\d][\d.\s\xa0]*)\s*€", text)
    if not m:
        return None
    raw = re.sub(r"[\s\xa0.]", "", m.group(1))  # retire espaces ET points (milliers)
    try:
        val = float(raw)
        return val if val >= 1000 else None
    except ValueError:
        return None


def _span_int(card, selector: str) -> int | None:
    el = card.select_one(selector)
    if not el:
        return None
    m = re.search(r"\d+", el.get_text(strip=True))
    return int(m.group(0)) if m else None


def _span_surface(card, selector: str) -> float | None:
    """'4.013m²' → 4013.0 ; '563m²' → 563.0 (point = séparateur de milliers)."""
    el = card.select_one(selector)
    if not el:
        return None
    txt = el.get_text(strip=True)
    m = re.search(r"([\d.\s\xa0]+)\s*m", txt)
    if not m:
        return None
    raw = re.sub(r"[\s\xa0.]", "", m.group(1))
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _parse_surface_hab(text: str) -> float | None:
    """Cherche 'NNN m² (habitable/hab/de)' dans le texte libre."""
    if not text:
        return None
    m = re.search(
        r"(\d[\d\s\xa0]*)\s*m(?:²|2|\s*²)?\s*(?:hab|habitable|habitables|de surface)",
        text,
        re.IGNORECASE,
    )
    if not m:
        # 'de 540 m²' générique
        m = re.search(r"\bde\s+(\d[\d\s\xa0]*)\s*m(?:²|2)\b", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 3000:
                return f
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
    print(f"\nTotal Agence du Périgord : {len(biens)} annonces")
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

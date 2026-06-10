"""scrapers/bonne_etoile.py — Bonne Étoile Immobilier (réseau de mandataires)

Méthode : scrape_simple (httpx) — SSR HTML (template TwImmo / twimmopro)
Site : https://www.bonne-etoile-immobilier.fr

Listing : pas de pagination fiable sur /acheter (set "à la une" figé).
  → On ouvre une session via POST sur /immobilier/ (formulaire moteur, sans
    filtre = tout l'inventaire), puis on pagine en GET sur /immobilier/{N}.html
    (la session/cookie est nécessaire ; le N pagine réellement).
  Cartes : a[href^="/vente-...-NN-...html"] (le 2e token "-16-" du slug est l'ID
  réseau de l'agence, PAS le département → inutilisable pour filtrer).

Filtre département : AUCUN slug/param dept fiable côté liste (la ville seule est
  ambiguë, et le "16" du slug est l'ID agence). On récupère donc le code postal
  sur la PAGE DÉTAIL, où le site expose un champ JS `resumeannonce` du type :
    "Vente maison - 45480 Autruy-sur-Juine - 7 pièces - 215 m² - 380 000 €"
  → CP exact, ville, type, pièces, surface et prix. POST-FILTRE STRICT sur
  code_postal[:2] == dept (0 fuite hors-zone). Inventaire national mais faible :
  l'essentiel est en Haute-Marne (52), Corse, Paris, Marseille ; dans la zone
  cible on trouve quelques biens (72 Le Mans/Coulaines, 45 Loiret…).

Détail (enrichissement, même fetch) :
  - DPE     : "Classe énergie X"
  - Photos  : https://medias.twimmopro.com/...-photo-hd.webp
  - Terrain : "terrain : NNNN m²" dans le corps
  - Desc    : meta og:description + corps .offer-description

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.bonne-etoile-immobilier.fr"
MAX_PAGES = 8
PHOTOS_PER_CARD = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Lien d'une carte d'annonce (le NN après la ville est l'ID agence, pas le dept)
_CARD_HREF = re.compile(r"^/(?:annonce-)?vente-(maison|appartement)-\d", re.IGNORECASE)

# Types à conserver (on ne garde pas les appartements)
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps-de-ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|studio",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    dept_set = set(departements)
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1) Ouvre la session (formulaire moteur sans filtre = inventaire complet)
        try:
            await client.post(
                f"{BASE_URL}/immobilier/",
                data={"moteur[type]": "", "moteur[categorie]": "", "moteur[prix]": ""},
            )
        except Exception as e:
            print(f"[BonneEtoile] Erreur ouverture session: {e}")
            return results

        # 2) Crawl de toutes les pages : on collecte les URL détail
        card_urls = await _collect_card_urls(client)
        print(f"[BonneEtoile] {len(card_urls)} annonces dans l'inventaire")

        # 3) Page détail (CP fiable) + post-filtre dept STRICT
        for href in card_urls:
            # Pré-filtre type sur le slug (évite des fetchs inutiles d'appartements)
            type_seg = href.lstrip("/")
            if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
                continue

            url = href if href.startswith("http") else BASE_URL + href
            try:
                bien = await _scrape_detail(client, url, href, dept_set)
            except Exception as e:
                print(f"[BonneEtoile] Erreur detail {href}: {e}")
                bien = None
            await asyncio.sleep(0.5)

            if not bien:
                continue

            # Post-filtre département STRICT (0 fuite)
            cp = bien.get("code_postal") or ""
            if not cp or cp[:2] not in dept_set:
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
            bien["departement"] = cp[:2]
            results.append(bien)
            print(
                f"[BonneEtoile] [{cp}] {bien['ville']} — {bien['prix']}€ "
                f"— {bien.get('surface') or '?'}m²"
            )

    return results


async def _collect_card_urls(client: httpx.AsyncClient) -> list[str]:
    seen: dict[str, None] = {}
    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/immobilier/" if page == 1 else f"{BASE_URL}/immobilier/{page}.html"
        try:
            r = await client.get(url)
        except Exception:
            break
        if r.status_code != 200:
            break

        soup = BeautifulSoup(r.text, "html.parser")
        added = 0
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if _CARD_HREF.match(href):
                if href not in seen:
                    seen[href] = None
                    added += 1

        # La pagination renvoie page 1 si on dépasse → plus de nouveaux → stop
        if added == 0 and page > 1:
            break
        await asyncio.sleep(0.4)

    return list(seen.keys())


async def _scrape_detail(
    client: httpx.AsyncClient, url: str, href: str, dept_set: set[str]
) -> dict | None:
    r = await client.get(url)
    if r.status_code != 200:
        return None
    t = r.text
    soup = BeautifulSoup(t, "html.parser")

    # Champ résumé JS : "Vente maison - 45480 Ville - 7 pièces - 215 m² - 380 000 €"
    m = re.search(r"resumeannonce\s*:\s*\"([^\"]+)\"", t)
    resume = m.group(1) if m else ""

    cp, ville = _parse_cp_ville(resume)
    if not cp:
        # secours : meta description "... 45480, ..."
        og = soup.find("meta", property="og:description")
        ogc = og.get("content", "") if og else ""
        m_cp = re.search(r"\b(\d{5})\b", ogc)
        cp = m_cp.group(1) if m_cp else ""

    # Inutile de parser plus loin si hors zone
    if not cp or cp[:2] not in dept_set:
        return None

    type_bien = _type_from_resume(resume) or _type_from_href(href)
    if not type_bien or not _KEEP_TYPE.search(type_bien):
        return None

    pieces = _parse_int(r"(\d+)\s*pièces?", resume)
    surface = _parse_surface(resume)
    prix = _parse_price_resume(resume)

    # id_annonce : ref du slug final (ex 1060v6105m)
    m_ref = re.search(r"-(\d+v\d+\w?)\.html$", href)
    id_annonce = m_ref.group(1) if m_ref else href

    # Titre
    h1 = soup.find("h1")
    titre = h1.get_text(" ", strip=True) if h1 else ""
    titre = re.sub(r"\s+", " ", titre).strip()
    if not titre:
        titre = (resume.split(" - ")[0] if resume else f"{type_bien} {ville}").strip()

    # Description : og:description + corps
    description = ""
    og = soup.find("meta", property="og:description")
    if og and og.get("content"):
        description = re.sub(r"\s+", " ", og["content"]).strip()
    body = soup.get_text(" ", strip=True)

    # Terrain : "terrain : NNNN m²"
    surface_terrain = _parse_terrain(body)

    # DPE : "Classe énergie X" (on ignore le GES)
    dpe = None
    m_dpe = re.search(r"Classe\s+énergie\s+([A-G])\b", t, re.IGNORECASE)
    if m_dpe:
        dpe = m_dpe.group(1).upper()

    # Photos
    photos = []
    for u in re.findall(
        r"https://medias\.twimmopro\.com/[^\"'\s]+?-photo-(?:hd|moyenne)\.(?:webp|jpg|jpeg|png)",
        t,
    ):
        if u not in photos:
            photos.append(u)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "bonne_etoile",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": cp[:2],
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Bonne Étoile Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_cp_ville(resume: str) -> tuple[str, str]:
    """'Vente maison - 45480 Autruy-sur-Juine - 7 pièces...' → ('45480', 'Autruy-sur-Juine')"""
    m = re.search(r"(\d{5})\s+([^\-|]+)", resume)
    if not m:
        return "", ""
    cp = m.group(1)
    ville = re.sub(r"\s+", " ", m.group(2)).strip(" -")
    return cp, ville


def _type_from_resume(resume: str) -> str:
    m = re.match(r"\s*(?:Vente|Achat)\s+([a-zàâéèêîôûç\-]+)", resume, re.IGNORECASE)
    return m.group(1).lower() if m else ""


def _type_from_href(href: str) -> str:
    m = re.match(r"/(?:annonce-)?vente-([a-z]+)-", href, re.IGNORECASE)
    return m.group(1).lower() if m else ""


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_surface(resume: str) -> float | None:
    m = re.search(r"([\d\s\xa0]+)\s*m²", resume)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 3000:
                return f
        except ValueError:
            pass
    return None


def _parse_price_resume(resume: str) -> float | None:
    # Le prix est le dernier nombre suivi de € dans le résumé
    matches = re.findall(r"([\d\s\xa0]+)\s*€", resume)
    if not matches:
        return None
    val = re.sub(r"[\s\xa0]", "", matches[-1])
    try:
        return float(val) if val else None
    except ValueError:
        return None


def _parse_terrain(body: str) -> float | None:
    m = re.search(r"terrain\s*:?\s*([\d\s\xa0]+)\s*m²", body, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 10 <= f <= 5_000_000:
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
    print(f"\nTotal Bonne Étoile: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — DPE {b.get('dpe') or '?'}"
            f" — {b['type_bien']} — {b['ville']}"
        )

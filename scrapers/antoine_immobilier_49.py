"""scrapers/antoine_immobilier_49.py — Antoine Immobilier (agence indépendante, Angers)

Méthode : scrape_simple (httpx) — SSR HTML (site classique, contenu dans le HTML brut).
Agence mono-département : Angers (place Hérault + rue Baudrière), Maine-et-Loire (49).
URL pattern : /achat,appartement,angers.html  (page catalogue VENTE unique)
              Les chemins /achat,maison,... /vente,maison,... existent mais sont
              VIDES (l'agence n'a que des appartements à la vente en stock).
              /nos-biens etc. → 404. Le sitemap.xml confirme les chemins catalogue.

Stratégie filtre département : l'agence opère uniquement sur Angers / agglomération
              (toutes les villes du catalogue sont en 49). On déduit la ville du slug
              d'URL (vente,{type},{ville},{ref}.html) et on mappe vers le code postal 49.
              Post-filtre STRICT : on ne garde que les villes connues en 49 (ou tout
              slug dont le CP commence par 49). departement forcé à "49" puis vérifié
              contre la liste des départements cibles → 0 fuite par construction.

Cartes : div.liste (chacune contenant a.fiche_detaillee)
  - URL   : a.fiche_detaillee[href]  → vente,{type},{ville},{ref}.html&content=1
  - Titre : .intitule  (a.fiche_detaillee dans <strong>)
  - Prix  : .display_prix_annonce  →  "234 876 €"
  - Desc  : .description_bien  (commence par <b>Réf. XXXX</b>)
  - Réf   : .description_bien b  →  "Réf. 3824AG"
  - Photo : img[src] (./images/biens/imageNNN_zoom_1.webp)
  - Surface / pièces : déduits du titre + description (texte libre, "75 m2", "T3").

Particularités : pagination ?page=N annoncée mais non fonctionnelle (renvoie 0 carte) ;
              le catalogue tient sur une seule page (~10 biens). Pas de CP dans la page
              (ajouté via mapping ville→CP). Photos en .webp.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int

BASE_URL = "https://www.antoine-immobilier.com"
# Page catalogue VENTE unique (les autres chemins de vente sont vides).
LIST_PATHS = [
    "/achat,appartement,angers.html",
    "/achat,maison,angers.html",
    "/vente,maison,angers.html",
    "/vente,appartement,angers.html",
    "/achat,local,angers.html",
]
MAX_PAGES = 6
PHOTOS_PER_CARD = 10


# Slug de ville (dans l'URL des annonces) → code postal (Maine-et-Loire, 49).
# Communes de l'agglomération d'Angers où l'agence est susceptible d'avoir des biens.
VILLE_CP_49: dict[str, str] = {
    "angers": "49000",
    "beaucouze": "49070",
    "avrille": "49240",
    "saintegemmessurloire": "49130",
    "pelouailleslesvignes": "49112",
    "ecouflant": "49000",
    "trelaze": "49800",
    "lesponts-de-ce": "49130",
    "lespontsdece": "49130",
    "saintbarthelemydanjou": "49124",
    "montreuiljuigne": "49460",
    "bouchemaine": "49080",
    "muronssaintleger": "49460",
    "verrieresenanjou": "49112",
    "feneu": "49460",
    "briollay": "49125",
    "cantenay-epinard": "49460",
    "cantenayepinard": "49460",
    "murslerable": "49510",
    "saintsylvaindanjou": "49480",
    "loiretabac": "49160",
}

_KEEP_TYPE = re.compile(
    r"maison|appartement|villa|propriete|propriété|loft|studio|duplex|"
    r"longere|longère|ferme|immeuble|hotel-particulier",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"terrain|garage|parking|local|commerce|bureau|fonds|cave|box",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # L'agence est mono-département (49). Si 49 n'est pas dans la cible → rien.
    if "49" not in departements:
        print("[Antoine] Dept 49 hors cible → 0 annonce")
        return []

    results: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for path in LIST_PATHS:
            try:
                biens = await _scrape_list(client, path)
            except Exception as e:
                print(f"[Antoine] Erreur {path}: {e}")
                continue

            for bien in biens:
                # Post-filtre STRICT : code postal en 49 ET département cible.
                cp = bien.get("code_postal") or ""
                if not cp or cp[:2] != "49":
                    continue
                if "49" not in departements:
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
                results.append(bien)

            await asyncio.sleep(0.5)

    print(f"[Antoine] Dept 49: {len(results)} annonces")
    return results


async def _scrape_list(client: httpx.AsyncClient, path: str) -> list[dict]:
    biens: list[dict] = []
    seen_page_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = BASE_URL + path + ("" if page == 1 else f"?page={page}")
        r = await client.get(url)
        if r.status_code != 200:
            break

        soup = BeautifulSoup(r.text, "html.parser")
        cards = [
            c for c in soup.select("div.liste") if c.select_one("a.fiche_detaillee")
        ]
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue
            if bien["id_annonce"] in seen_page_ids:
                continue
            seen_page_ids.add(bien["id_annonce"])
            biens.append(bien)
            new_on_page += 1

        if new_on_page == 0:
            break
        await asyncio.sleep(0.4)

    return biens


def _parse_card(card) -> dict | None:
    link = card.select_one("a.fiche_detaillee")
    href = (link.get("href", "") if link else "").strip()
    if not href:
        return None

    # href : "vente,appartement,angers,3824AG.html&content=1"
    clean_href = href.lstrip("/")
    if clean_href.startswith("http"):
        url = clean_href
        path_part = clean_href.split("/")[-1]
    else:
        url = f"{BASE_URL}/{clean_href}"
        path_part = clean_href

    # Segments : transaction , type , ville , ref.html...
    head = path_part.split(".html")[0]
    parts = head.split(",")
    if len(parts) < 4:
        return None
    type_seg = parts[1].lower()
    ville_slug = parts[2].lower()
    ref_slug = parts[3]

    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg

    code_postal = VILLE_CP_49.get(ville_slug, "")
    if not code_postal:
        # Ville inconnue : on n'invente pas de département → on écarte (0 fuite).
        return None
    ville = _slug_to_ville(ville_slug)

    # Référence (id_annonce)
    ref_el = card.select_one(".description_bien b")
    ref_txt = ref_el.get_text(strip=True) if ref_el else ""
    m_ref = re.search(r"R[ée]f\.?\s*([A-Za-z0-9-]+)", ref_txt)
    id_annonce = (m_ref.group(1) if m_ref else "") or ref_slug or url

    # Titre
    title_el = card.select_one(".intitule")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Description (on retire le préfixe "Réf. XXXX")
    desc_el = card.select_one(".description_bien")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""
    description = re.sub(r"^\s*R[ée]f\.?\s*[A-Za-z0-9-]+\s*", "", description).strip()

    # Prix
    price_el = card.select_one(".display_prix_annonce") or card.select_one(
        ".prix_annonce"
    )
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Surface (texte libre : "75 m2", "30,40 m2")
    surface = _parse_surface(titre) or _parse_surface(description)

    # Pièces : "T3" dans le titre, ou "X pièces"
    pieces = _parse_pieces(titre) or _parse_pieces(description)
    chambres = _parse_int(r"(\d+)\s*chambre", description)

    # Photos
    photos = []
    for img in card.select("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:"):
            continue
        if "images/biens" not in src and "/biens/" not in src:
            continue
        if src.startswith("./"):
            src = src[2:]
        if src.startswith("//"):
            src = "https:" + src
        elif not src.startswith("http"):
            src = f"{BASE_URL}/{src.lstrip('/')}"
        photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "antoine_immobilier_49",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": "49",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Antoine Immobilier (Angers)",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _slug_to_ville(slug: str) -> str:
    overrides = {
        "saintegemmessurloire": "Sainte-Gemmes-sur-Loire",
        "pelouailleslesvignes": "Pellouailles-les-Vignes",
        "lespontsdece": "Les Ponts-de-Cé",
        "lesponts-de-ce": "Les Ponts-de-Cé",
        "saintbarthelemydanjou": "Saint-Barthélemy-d'Anjou",
        "montreuiljuigne": "Montreuil-Juigné",
        "cantenayepinard": "Cantenay-Épinard",
        "verrieresenanjou": "Verrières-en-Anjou",
        "saintsylvaindanjou": "Saint-Sylvain-d'Anjou",
    }
    if slug in overrides:
        return overrides[slug]
    return slug.replace("-", " ").title()


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text.split(",")[0] if "," in text else text)
    # text type "234 876 €" → garder les chiffres
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """Surface habitable. On ignore les '… m2' précédés de terrain/parcelle/jardin."""
    if not text:
        return None
    # Priorité aux mentions explicites d'habitable / carrez / au sol.
    for pat in (
        r"(\d+(?:[.,]\d+)?)\s*m\s*[²2]\s*(?:carrez|habitable|hab\b|au sol)",
        r"(?:de|environ|d['’]\s*environ)\s*(\d+(?:[.,]\d+)?)\s*m\s*[²2]",
    ):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            f = _safe_surface(m.group(1))
            if f is not None:
                return f
    # Sinon, premier 'N m²' qui n'est PAS un terrain/parcelle/jardin/dépendance.
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*m\s*[²2]", text, re.IGNORECASE):
        prefix = text[max(0, m.start() - 60):m.start()].lower()
        if re.search(
            r"terrain|parcelle|jardin|d[ée]pendance|garage|cour\b|terrasse|"
            r"sur une parcelle|de plus de",
            prefix,
        ):
            continue
        f = _safe_surface(m.group(1))
        if f is not None:
            return f
    return None


def _safe_surface(val: str) -> float | None:
    try:
        f = float(val.replace(",", "."))
        return f if 8 <= f <= 2000 else None
    except ValueError:
        return None


def _parse_pieces(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"\bT\s?(\d+)\b", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*pi[eè]ce", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
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
    print(f"\nTotal Antoine Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

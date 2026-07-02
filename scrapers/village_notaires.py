"""scrapers/village_notaires.py — Village des Notaires et du Patrimoine

Méthode : scrape_simple (httpx) — SSR HTML (pas de JS nécessaire).

Agrégateur d'annonces immobilières des offices notariaux (republie le flux
immonot, mais sur son propre domaine village-notaires-patrimoine.com — source
distincte ; la déduplication du hunter fusionnera tout recoupement avec immonot).

URL pattern : /-les-annonces-immobilieres-des-notaires-/{NN}/MAIS
              → filtre département CÔTÉ SERVEUR (vérifié : aucune fuite hors-dept,
                tous les codes postaux renvoyés commencent par {NN}).
              {NN} = numéro de département, MAIS = catégorie « maison » du site
              (renvoie en réalité maisons + quelques appartements/locations que
              l'on re-filtre sur le préfixe de l'intitulé h4).

Cartes : div.col-xs-12.mb-2 (≈ 50 par département, page unique)
  - Lien détail : a[href] (vers www.immonot.com/annonce-immobiliere/...)
  - Photo       : img[src] (photos.immonot.com)
  - Prix        : <p> « Prix : <strong>345 840.00  €</strong> »
  - Intitulé    : h4.mt-1  →  « Achat maison - Ville (CODEPOSTAL) »
  - Description  : texte de la carte (après l'intitulé)

On ne conserve que les intitulés « Achat maison/propriete/longere/manoir/... » ;
les locations et appartements sont écartés. Surface habitable extraite du texte
(« superficie habitable de 106 m² », « de 150 m² »…).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int

BASE_URL = "https://www.village-notaires-patrimoine.com"
PHOTOS_PER_CARD = 6


# Départements cibles → le slug d'URL est simplement le numéro de département.
DEPT_SLUGS: dict[str, str] = {
    "72": "72",
    "28": "28",
    "45": "45",
    "89": "89",
    "49": "49",
    "37": "37",
    "36": "36",
    "18": "18",
    "58": "58",
    "41": "41",
    "53": "53",
}

# Intitulés (préfixe avant « - Ville ») à conserver : achats de biens « maison ».
_KEEP_TITLE = re.compile(
    r"achat\s+(maison|propriete|propriété|longere|longère|manoir|chateau|"
    r"château|ferme|demeure|domaine|moulin|villa|corps\s+de\s+ferme|"
    r"maison\s+de\s+village)",
    re.IGNORECASE,
)
# Intitulés explicitement écartés (location, appartement, terrain, commerce…).
_EXCLUDE_TITLE = re.compile(
    r"location|appartement|terrain|fonds|murs?\s+commerc|immeuble|garage|"
    r"parking|local|bureau|viager",
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
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, slug, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[VillageNotaires] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[VillageNotaires] Erreur dept {dept}: {e}")
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

    url = f"{BASE_URL}/-les-annonces-immobilieres-des-notaires-/{slug}/MAIS"
    r = await client.get(url)
    if r.status_code != 200:
        return biens

    cards = BeautifulSoup(r.text, "html.parser").select("div.col-xs-12.mb-2")
    for card in cards:
        try:
            bien = _parse_card(card, dept)
        except Exception:
            continue
        if not bien:
            continue

        # Sécurité : on n'accepte que le département cible (filtre serveur déjà OK)
        if bien["code_postal"] and bien["code_postal"][:2] != dept:
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

    return biens


def _parse_card(card, dept: str) -> dict | None:
    h4 = card.find("h4")
    h4_text = h4.get_text(" ", strip=True) if h4 else ""
    if not h4_text:
        return None

    # « Achat maison - Ville (45140) »  → préfixe avant le 1er « - »
    prefix = h4_text.split(" - ")[0].strip()
    if _EXCLUDE_TITLE.search(prefix) or not _KEEP_TITLE.search(prefix):
        return None
    type_bien = re.sub(r"^achat\s+", "", prefix, flags=re.IGNORECASE).strip().lower()

    # Localisation : dernière parenthèse (CP) de l'intitulé
    code_postal = ""
    m_cp = re.search(r"\((\d{5})\)", h4_text)
    if m_cp:
        code_postal = m_cp.group(1)
    # Ville : entre le 1er « - » et la parenthèse du CP
    ville = ""
    after = h4_text.split(" - ", 1)[1] if " - " in h4_text else h4_text
    ville = re.sub(r"\s*\(\d{5}\).*$", "", after).strip()

    # Lien détail (vers immonot) + id_annonce depuis l'URL
    link = card.find("a", href=True)
    href = link["href"].strip() if link else ""
    url = href if href.startswith("http") else (BASE_URL + href if href else "")
    id_annonce = ""
    m_id = re.search(r"/(l\d+)/", url) or re.search(r"/annonce[- ]?immobiliere/([^/]+)/", url)
    if m_id:
        id_annonce = m_id.group(1)
    if not id_annonce:
        id_annonce = url or h4_text

    # Texte complet de la carte (sert de description + extraction surface/réf)
    full_text = card.get_text(" ", strip=True)
    description = re.sub(r"\s*Voir l'annonce.*$", "", full_text).strip()
    # Retire l'en-tête « Prix : … Date de création : … {intitulé} »
    description = re.sub(r"^.*?\(\d{5}\)\s*", "", description, count=1).strip()

    titre = h4_text[:150]

    # Prix : « Prix : <strong>345 840.00 €</strong> »
    prix = None
    p_el = card.find("strong")
    if p_el:
        prix = _parse_price(p_el.get_text(" ", strip=True))
    if prix is None:
        m_p = re.search(r"Prix\s*:\s*([\d\s\xa0.,]+)\s*€", full_text)
        if m_p:
            prix = _parse_price(m_p.group(1))

    # Surface habitable depuis le texte libre
    surface = _parse_surface_hab(full_text)
    # Terrain éventuel
    surface_terrain = _parse_terrain(full_text)
    # Pièces / chambres éventuelles
    pieces = _parse_int(r"(\d+)\s*pi[eè]ces?", full_text)
    chambres = _parse_int(r"(\d+)\s*chambres?", full_text)

    # Photos
    photos = []
    for img in card.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "village_notaires",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre,
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Notaire",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    # « 345 840.00 » → 345840.0  ; gère séparateurs espace/insécable.
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    # Supprime un éventuel « .00 » décimal final puis tout non-chiffre
    cleaned = re.sub(r"[.,]\d{2}$", "", cleaned)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_terrain(text: str) -> float | None:
    """« terrain de 412 m² » / « terrain de 1 200 m² » → 412.0 / 1200.0"""
    m = re.search(r"terrain[^0-9]{0,15}([\d\s\xa0]+)\s*m", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 1 <= f <= 5_000_000:
                return f
        except ValueError:
            pass
    return None


def _parse_surface_hab(text: str) -> float | None:
    """Cherche « habitable de NNN m² » / « surface de NNN m² » / « NNN m² hab »."""
    if not text:
        return None
    # NB : on évite les fallback trop larges (« parcelle de 1201 m² ») qui
    # captureraient le terrain ; on n'accepte que des contextes « surface
    # habitable » explicites.
    patterns = [
        r"habitable[^0-9]{0,12}(\d[\d\s\xa0]*)\s*m",
        r"surfac[a-z]*[^0-9]{0,12}(\d[\d\s\xa0]*)\s*m",
        r"superfici[a-z]*[^0-9]{0,12}(\d[\d\s\xa0]*)\s*m",
        r"(\d[\d\s\xa0]*)\s*m²?\s*(?:hab|habitable)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = re.sub(r"[\s\xa0]", "", m.group(1))
            try:
                f = float(val)
                if 8 <= f <= 2000:
                    return f
            except ValueError:
                continue
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
    print(f"\nTotal Village des Notaires: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

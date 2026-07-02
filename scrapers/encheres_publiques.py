"""scrapers/encheres_publiques.py — Enchères Publiques (encheres-publiques.com)

Segment : ventes aux enchères immobilières (saisies judiciaires, liquidations,
          ventes volontaires) — portail national des avocats / tribunaux.

Méthode : api_inoff (httpx) — SSR Next.js. Le HTML embarque un cache Apollo
          GraphQL complet dans <script id="__NEXT_DATA__"> :
            props.pageProps.apolloState.data → entités "Lot:" et "Adresse:".
          On lit ce JSON (pas de Playwright nécessaire, pas d'API à signer).

URL pattern : /ventes/immobilier/maisons/{region-slug}
          → filtre CÔTÉ SERVEUR par RÉGION (le param ?departement=NN ne filtre
            PAS ; ?page=N est purement client → tout le stock régional tient
            dans une seule réponse SSR). On mappe chaque département cible vers
            sa région, puis on POST-FILTRE strictement par code département.

Filtre département : le code dept est le suffixe du slug de ville présent dans
          chaque href de carte rendue :
            /encheres/immobilier/maisons/{ville-DD}/{slug}_{ID}   (ex: champigny-89)
          → fiable et présent sur 100 % des cartes affichées. On joint chaque
            href (qui porte dept + id) au Lot:{ID} du cache Apollo pour les
            données structurées. Les Lot:" orphelins (sans href rendu, prefetch
            partiel sans adresse) sont ignorés → aucune fuite possible.

Lot (apolloState) :
  - id, nom (titre, contient souvent la surface)
  - criteres_resume : "Ville · 137 m² · 7 pièces · 438 €/m²" (parties optionnelles)
  - mise_a_prix : prix de DÉPART de l'enchère (en €) → champ prix
  - photo : chemin streetview/photo → URL absolue
  - sous_categorie : "maisons"
Adresse (adresse_defaut) : ville, ville_slug, region.

Particularité : c'est un prix de MISE À PRIX (enchère), pas un prix de vente
                ferme — borne prix_max appliquée dessus à titre indicatif.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx

from scrapers._base import HEADERS

BASE_URL = "https://www.encheres-publiques.com"
SOUS_CATEGORIE = "maisons"
PHOTOS_PER_CARD = 5


# Code département → slug de région (filtre serveur encheres-publiques.com).
# Couvre la zone Val-de-Loire / Ouest / Bourgogne + régions limitrophes utiles.
DEPT_REGION: dict[str, str] = {
    # Centre-Val de Loire
    "18": "centre-val-de-loire",
    "28": "centre-val-de-loire",
    "36": "centre-val-de-loire",
    "37": "centre-val-de-loire",
    "41": "centre-val-de-loire",
    "45": "centre-val-de-loire",
    # Pays de la Loire
    "44": "pays-de-la-loire",
    "49": "pays-de-la-loire",
    "53": "pays-de-la-loire",
    "72": "pays-de-la-loire",
    "85": "pays-de-la-loire",
    # Bourgogne-Franche-Comté
    "21": "bourgogne-franche-comte",
    "58": "bourgogne-franche-comte",
    "71": "bourgogne-franche-comte",
    "89": "bourgogne-franche-comte",
}

# href de carte : /encheres/immobilier/maisons/{ville}-{DD}/{slug}_{ID}
_HREF_RE = re.compile(
    r"/encheres/immobilier/maisons/([a-z0-9-]+)-(\d{2,3})/([a-z0-9-]+)_(\d+)"
)
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    # Regrouper les départements cibles par région (1 requête / région).
    regions: dict[str, set[str]] = {}
    for dept in departements:
        region = DEPT_REGION.get(dept)
        if region:
            regions.setdefault(region, set()).add(dept)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for region, wanted in regions.items():
            try:
                biens = await _scrape_region(
                    client, region, wanted, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                by_dept: dict[str, int] = {}
                for b in biens:
                    by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
                print(
                    f"[EncheresPubliques] Région {region}: "
                    f"{len(biens)} annonces {by_dept}"
                )
            except Exception as e:
                print(f"[EncheresPubliques] Erreur région {region}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_region(
    client: httpx.AsyncClient,
    region: str,
    wanted: set[str],
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    url = f"{BASE_URL}/ventes/immobilier/{SOUS_CATEGORIE}/{region}"
    r = await client.get(url)
    if r.status_code != 200:
        print(f"[EncheresPubliques] {region}: HTTP {r.status_code}")
        return []

    data = _extract_apollo(r.text)
    if not data:
        return []

    biens: list[dict] = []
    seen_ids: set[str] = set()

    # On itère les hrefs RENDUS (chacun porte dept + id) → pas de Lot orphelin.
    for m in _HREF_RE.finditer(r.text):
        ville_slug, dept, slug, lot_id = (
            m.group(1),
            m.group(2),
            m.group(3),
            m.group(4),
        )
        # POST-FILTRE DÉPARTEMENT STRICT (0 fuite).
        if dept not in wanted:
            continue
        if lot_id in seen_ids:
            continue
        seen_ids.add(lot_id)

        lot = data.get(f"Lot:{lot_id}")
        if not lot:
            continue

        bien = _parse_lot(lot, data, dept, ville_slug, slug, lot_id)
        if not bien:
            continue

        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        biens.append(bien)

    return biens


def _parse_lot(
    lot: dict,
    data: dict,
    dept: str,
    ville_slug: str,
    slug: str,
    lot_id: str,
) -> dict | None:
    url = f"{BASE_URL}/encheres/immobilier/{SOUS_CATEGORIE}/{ville_slug}-{dept}/{slug}_{lot_id}"

    titre = (lot.get("nom") or "").strip()

    # Ville / code dept fiables via l'adresse liée (sinon depuis le slug).
    ville = ""
    adr_ref = (lot.get("adresse_defaut") or {}).get("__ref")
    if adr_ref:
        adr = data.get(adr_ref) or {}
        ville = (adr.get("ville") or "").strip()
    if not ville:
        ville = ville_slug.replace("-", " ").title()

    # criteres_resume : "Ville · 137 m² · 7 pièces · 438 €/m²" (parties optionnelles)
    resume = lot.get("criteres_resume") or ""
    surface = _parse_surface(resume) or _parse_surface(titre)
    pieces = _parse_pieces(resume)

    # Prix = mise à prix (prix de départ de l'enchère).
    prix = lot.get("mise_a_prix")
    try:
        prix = float(prix) if prix not in (None, "") else None
    except (TypeError, ValueError):
        prix = None

    # Type de bien depuis le titre (saisie = libellés variés).
    type_bien = _guess_type(titre)

    # Photo (streetview ou photo réelle) → URL absolue.
    photos: list[str] = []
    photo = lot.get("photo")
    if photo:
        photos.append(photo if photo.startswith("http") else BASE_URL + photo)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "encheres_publiques",
        "url": url,
        "id_annonce": lot_id,
        "titre": titre[:150] or f"{type_bien.title()} {ville}".strip(),
        "type_bien": type_bien,
        "description": resume[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": None,  # non exposé ; dept fiable via slug
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Enchères Publiques (vente judiciaire/volontaire)",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────


def _extract_apollo(html: str) -> dict:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return {}
    try:
        d = json.loads(m.group(1))
        return d["props"]["pageProps"]["apolloState"]["data"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


def _parse_surface(text: str) -> float | None:
    if not text:
        return None
    # "137 m²" ou "84.01 m²" ou "133,54 m²" — on évite le "€/m²".
    m = re.search(r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m²(?!\s*[·]?\s*\d*\s*€)", text)
    if not m:
        m = re.search(r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
        try:
            f = float(val)
            if 5 <= f <= 5000:
                return f
        except ValueError:
            pass
    return None


def _parse_pieces(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d+)\s*pi[eè]ces?", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


# Mots-maison explicites prioritaires (évite qu'un nom de rue type
# "rue du Château d'Eau" ne fasse passer une maison en "propriete").
_HOUSE_RE = re.compile(r"\b(maison|pavillon|villa|long[eè]re|ferme|corps de ferme)\b", re.I)
_TYPE_PATTERNS = [
    (re.compile(r"appartement", re.I), "appartement"),
    (re.compile(r"\bstudio\b", re.I), "studio"),
    (re.compile(r"\bimmeuble\b", re.I), "immeuble"),
    (re.compile(r"\bvilla\b", re.I), "villa"),
    (re.compile(r"\b(ch[aâ]teau|manoir|demeure|propri[eé]t[eé])\b", re.I), "propriete"),
]


def _guess_type(titre: str) -> str:
    # Un mot-maison explicite l'emporte sur un château/manoir mentionné
    # incidemment (nom de rue, lieu-dit).
    if _HOUSE_RE.search(titre):
        return "maison"
    for rx, label in _TYPE_PATTERNS:
        if rx.search(titre):
            return label
    return "maison"  # sous-catégorie de la liste = maisons


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
    print(f"\nTotal Enchères Publiques: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€ (mise à prix)"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

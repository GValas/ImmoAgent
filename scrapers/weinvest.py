"""scrapers/weinvest.py — We Invest France (réseau hybride ~700 conseillers)

Méthode : api_inoff (httpx) — l'app weinvest.fr est un site **Bubble.io** rendu
côté client (SSR quasi vide). Pas besoin de Playwright : la **Data API Bubble**
est publiquement exposée à `/api/1.1/obj/property` et renvoie tout le catalogue
en JSON (type `property` listé dans `/api/1.1/meta`).

  - Note : bl-agents.fr (BL Agents, absorbé) redirige vers weinvest.fr.
  - Route publique des annonces (pour l'URL détail) : /bien-immobilier/{Slug}
    (cf. sitemap-bien-immobilier.xml). /acheter, /recherche → 404.

Filtre département : la Data API accepte des **constraints**. On filtre côté
serveur sur `code_postal` par plage texte ([dept]000 ≤ code_postal < [dept+1]000),
PUIS on re-vérifie strictement `code_postal[:2] == dept` côté Python → 0 fuite.

Champs JSON utiles : code_postal, ville, prix, surface_habitable, surface_terrain,
nombre_pieces, nombre_chambres, dpe_energie_label, product_type_text (liste CSV
"Maison,Parking,..."), titre_annonce, description (HTML), image_principale,
type_offre (1 = vente), ref, Slug, mandataire_denomination.

On ne garde que les **ventes** (type_offre == 1) dont le `product_type_text`
contient une typologie maison/propriété (exclut Appartement/Terrain/Commercial/
Stationnement/Immeuble purs).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx

BASE_URL = "https://weinvest.fr"
API_URL = f"{BASE_URL}/api/1.1/obj/property"
PAGE_LIMIT = 100          # max Bubble par requête
MAX_PER_DEPT = 600        # garde-fou
PHOTOS_PER_CARD = 1       # la Data API ne renvoie que l'image principale

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Typologies à conserver (sous-chaîne dans product_type_text, insensible casse)
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|longere|longère|manoir|chateau|château|"
    r"ferme|domaine|moulin|demeure|mas|gite|gîte",
    re.IGNORECASE,
)
# Typologies explicitement exclues (si présentes SANS une typologie maison)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|commercial|stationnement|parking|immeuble|local|"
    r"bureau|fonds|garage",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=40
    ) as client:
        for dept in departements:
            try:
                biens = await _scrape_dept(
                    client, dept, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[WeInvest] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[WeInvest] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.5)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    # Plage texte sur code_postal. Bubble n'expose que "greater than" (strict),
    # donc la borne basse vaut "{dept-1}999" pour inclure le CP "{dept}000"
    # (ex. 45000 Orléans). La borne haute "{dept+1}000" (strict) exclut le dept
    # suivant. Le post-filtre code_postal[:2] == dept verrouille ensuite tout.
    lo = f"{int(dept) - 1:02d}999"
    hi = f"{int(dept) + 1:02d}000"
    constraints = json.dumps(
        [
            {"key": "code_postal", "constraint_type": "greater than", "value": lo},
            {"key": "code_postal", "constraint_type": "less than", "value": hi},
        ]
    )

    biens: list[dict] = []
    seen_ids: set[str] = set()
    cursor = 0

    while cursor < MAX_PER_DEPT:
        r = await client.get(
            API_URL,
            params={"constraints": constraints, "limit": PAGE_LIMIT, "cursor": cursor},
        )
        if r.status_code != 200:
            break
        resp = r.json().get("response", {})
        rows = resp.get("results", [])
        if not rows:
            break

        for row in rows:
            try:
                bien = _parse_row(row, dept)
            except Exception:
                continue
            if not bien:
                continue

            # Post-filtre dept STRICT : 0 fuite hors-zone
            cp = bien["code_postal"]
            if not cp or cp[:2] != dept:
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

        if resp.get("remaining", 0) <= 0:
            break
        cursor += resp.get("count", len(rows))
        await asyncio.sleep(0.4)

    return biens


def _parse_row(row: dict, dept: str) -> dict | None:
    # Vente uniquement (type_offre == 1)
    if row.get("type_offre") not in (1, "1", None):
        return None

    # Typologie : on garde maisons/propriétés ; on exclut appart/terrain/etc. seuls
    ptype = row.get("product_type_text") or ""
    if not _KEEP_TYPE.search(ptype):
        if _EXCLUDE_TYPE.search(ptype):
            return None
        # type vide/inconnu → on exclut par prudence
        return None
    type_bien = (ptype.split(",")[0] or "maison").strip().lower()

    cp = str(row.get("code_postal") or "").strip()
    ville = (row.get("ville") or "").strip()

    slug = row.get("Slug") or row.get("temp_slug") or ""
    if slug:
        url = f"{BASE_URL}/bien-immobilier/{slug}"
    else:
        url = f"{BASE_URL}/bien-immobilier"

    ref = (row.get("ref") or row.get("id") or "").strip()
    id_annonce = row.get("_id") or ref or slug or url

    titre = (row.get("titre_annonce") or row.get("titreia_text") or "").strip()
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    description = _strip_html(row.get("description") or row.get("descriptionia_text") or "")

    prix = _to_float(row.get("prix"))
    surface = _to_float(row.get("surface_habitable"))
    surface_terrain = _to_float(row.get("surface_terrain")) or _to_float(row.get("surface_land"))
    pieces = _to_int(row.get("nombre_pieces"))
    chambres = _to_int(row.get("nombre_chambres"))

    dpe = (row.get("dpe_energie_label") or "").strip().upper() or None
    if dpe and dpe not in {"A", "B", "C", "D", "E", "F", "G"}:
        dpe = None

    photos = []
    img = row.get("image_principale") or row.get("image_small1_text")
    if img:
        if img.startswith("//"):
            img = "https:" + img
        photos.append(img)
    photos = photos[:PHOTOS_PER_CARD]

    agence = (row.get("mandataire_denomination") or "").replace("-", " ").strip()
    agence = f"We Invest ({agence})" if agence else "We Invest"

    return {
        "source": "weinvest",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": agence[:120],
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = re.sub(r"&#?\w+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _to_float(v) -> float | None:
    if v in (None, "", 0, "0"):
        return None
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _to_int(v) -> int | None:
    if v in (None, "", 0, "0"):
        return None
    try:
        i = int(float(v))
        return i if i > 0 else None
    except (TypeError, ValueError):
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
    print(f"\nTotal We Invest: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']} — DPE {b['dpe']}"
        )

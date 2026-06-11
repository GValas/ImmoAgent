"""scrapers/power_immo.py — Power Immo (réseau de mandataires immobiliers)

Site : https://www.power-immo.com (réseau national d'agents mandataires,
       ~350 mandataires, CMS « Twenty-Mille » / power_agences).

Méthode : api_inoff — la page liste (liste-annonces-vente.html) est rendue
          côté client (0 € dans le HTML brut) ; les annonces sont servies par
          un endpoint AJAX JSON interne. On l'appelle directement en httpx,
          sans Playwright.

Endpoint résultats (POST) :
  https://www.power-immo.com/immobilier/core/ajax_resultats.php
      ?type=vente&param_supp={"contour":"","tri":"date_desc"}
  body (form, notation tableau PHP) :
      global_search[localisations][]=d-72   ← filtre département CÔTÉ SERVEUR
      limit=24
      offset=N*24
  → JSON {"status":"success","response":[ {annonce…}, …, {"vente":"<total>"} ]}
  Le dernier élément de `response` porte le compteur (clé "vente"), à ignorer.

Filtre département : le jeton de localité « d-72 » (département 72) est résolu
  par l'autocomplete /immobilier/core/ajax_recherche_localisation.php?term=sarthe
  → [{"text":"Sarthe ( 72 )","id":"d-72"}]. Vérifié : chaque jeton d-NN ne
  renvoie QUE le département NN (0 fuite). Re-vérifié en plus par cp[:2] == dept.

Champs JSON par annonce :
  - id            → id_annonce + slug photo
  - entete        → "<span class='type_bien'>Maison</span> … <span
                     class='localisation'>18100  Vierzon</span>" (type + CP + ville)
  - titre         → titre court
  - description   → description complète (souvent longue)
  - prix          → "76 950" (chaîne, € séparés espaces)
  - surface       → "119" (m² habitables)
  - pictos        → {"pieces":"7","chambres":"4"}
  - url_annonce   → "annonces-vente/ventes-maisons-t7-vierzon/1044218.html"
  - cp, ville     → code postal + ville (doublon de l'entête, plus fiable)

Photo : déduite de l'id → /clients/power_agences/images/annonces-vente/principale/{id}.jpg
        (galerie complète enrichie ensuite par scrapers/gallery.py).

Couverture (test 2026-06-10, départements cibles) : 18→68, 36→54, 58→135,
  89→43, 45→12, 37→2, 49→1, 41→1 ; 72/53/28 → 0 (pas d'implantation actuelle).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import html
import json
import re

import httpx

BASE_URL = "https://www.power-immo.com"
AJAX_URL = f"{BASE_URL}/immobilier/core/ajax_resultats.php"
PAGE_SIZE = 24
MAX_PAGES = 12  # 12 * 24 = 288 annonces max par dept (large marge)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE_URL}/liste-annonces-vente.html",
}

_PARAM_SUPP = json.dumps({"contour": "", "tri": "date_desc"})

# Types de bien à conserver (depuis le <span class='type_bien'>)
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|corps de ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|viager",
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
        for dept in departements:
            try:
                biens = await _scrape_dept(
                    client, dept, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[PowerImmo] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[PowerImmo] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()
    token = f"d-{int(dept)}"  # d-72, d-9 → on garde le format brut du site

    for page in range(MAX_PAGES):
        data = {
            "global_search[localisations][]": token,
            "limit": PAGE_SIZE,
            "offset": PAGE_SIZE * page,
        }
        try:
            r = await client.post(
                AJAX_URL,
                params={"type": "vente", "param_supp": _PARAM_SUPP},
                data=data,
            )
        except httpx.HTTPError:
            break
        if r.status_code != 200:
            break

        try:
            payload = r.json()
        except (json.JSONDecodeError, ValueError):
            break
        items = payload.get("response") or []
        if not items:
            break

        new_on_page = 0
        for item in items:
            if not isinstance(item, dict) or "id" not in item:
                continue  # dernier élément = compteur {"vente": "N"}
            try:
                bien = _parse_item(item, dept)
            except Exception:
                continue
            if not bien:
                continue

            # Sécurité : filtre serveur déjà OK, on re-vérifie le CP
            cp = bien["code_postal"]
            if cp and cp[:2] != dept:
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

        # Moins d'une page pleine → dernière page atteinte
        real_items = [i for i in items if isinstance(i, dict) and "id" in i]
        if len(real_items) < PAGE_SIZE:
            break
        if new_on_page == 0:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_item(item: dict, dept: str) -> dict | None:
    # Type de bien depuis l'entête
    entete = item.get("entete", "") or ""
    m_type = re.search(r"type_bien'>([^<]+)<", entete)
    type_raw = (m_type.group(1).strip() if m_type else "") or item.get("type", "")
    type_clean = type_raw.strip()
    if _EXCLUDE_TYPE.search(type_clean):
        return None
    if not _KEEP_TYPE.search(type_clean):
        return None
    type_bien = type_clean.lower() or "maison"

    # CP / ville : champs dédiés (plus fiables que l'entête)
    code_postal = str(item.get("cp", "") or "").strip()
    if not code_postal:
        m_cp = re.search(r"(\d{5})", entete)
        code_postal = m_cp.group(1) if m_cp else ""
    ville = html.unescape(str(item.get("ville", "") or "")).strip()
    if not ville:
        m_v = re.search(r"localisation'>\d{5}\s+([^<]+)<", entete)
        ville = html.unescape(m_v.group(1)).strip() if m_v else ""

    # URL détail
    url_rel = item.get("url_annonce", "") or ""
    url = url_rel if url_rel.startswith("http") else f"{BASE_URL}/{url_rel.lstrip('/')}"

    aid = str(item.get("id", "") or "").strip()
    reference = str(item.get("reference", "") or "").strip()
    id_annonce = reference or aid or url

    titre = html.unescape(str(item.get("titre", "") or "")).strip()
    description = html.unescape(str(item.get("description", "") or "")).strip()
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    prix = _parse_num(item.get("prix"))
    surface = _parse_num(item.get("surface"))

    pictos = item.get("pictos") or {}
    pieces = _to_int(pictos.get("pieces"))
    chambres = _to_int(pictos.get("chambres"))

    # Photo principale déduite de l'id (galerie complète ensuite par gallery.py)
    photos: list[str] = []
    if aid and str(item.get("image_manquante", "0")) != "1":
        photos.append(
            f"{BASE_URL}/clients/power_agences/images/"
            f"annonces-vente/principale/{aid}.jpg"
        )

    return {
        "source": "power_immo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,  # absent de la liste ; gallery.py / texte l'extrait
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Power Immo",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_num(val) -> float | None:
    """'76 950' / '119' / 119 → float ; None si vide/non numérique."""
    if val is None:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(val).replace(",", "."))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _to_int(val) -> int | None:
    if val is None:
        return None
    m = re.search(r"\d+", str(val))
    return int(m.group(0)) if m else None


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
    print(f"\nTotal Power Immo: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p/{b['chambres'] or '?'}ch"
            f" — {b['type_bien']} — {b['ville']}"
        )

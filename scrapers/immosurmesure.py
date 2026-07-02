"""scrapers/immosurmesure.py — Immosurmesure (réseau de mandataires immobiliers)

Méthode : api_inoff (httpx) — la page liste est rendue côté client (Netty SaaS),
          MAIS le SSR embarque toutes les annonces dans un gros blob JSON encodé en
          base64 (`JSON.parse(b64_to_utf8("..."))`). On décode ce blob : aucune
          exécution JS, aucun Playwright requis.

URL pattern (filtre département CÔTÉ SERVEUR) :
    /vente/maison/departement-{slug}      (ex: /vente/maison/departement-nievre)
    → le blob expose `prodResults["search"]` = la liste des biens du département
      demandé (les autres groupes du blob = blocs « populaires »/promo nationaux,
      qu'on IGNORE → pas de fuite hors-zone). Re-vérif stricte `cp[:2] == dept`.

Blob produit (`{"prodId": {...}, "prodResults": {"search": [...]}, "prodUrl": ...}`) :
  Chaque bien (clé = prod_ref) expose :
    - cp / city / sector / geo_label   → localisation
    - surface / surface_carrez         → m² habitables
    - rooms / rooms2                   → pièces / chambres
    - land                             → terrain (m²)
    - prod_type ("house"/"appt"/"land"/"building"…)
    - type_house ("00" maison de ville/ancienne, etc.)
    - promo (1001 = VENDU) ; formated.price.amount (int si en vente, "Vendu" sinon)
    - url["fr"]                        → slug détail : /vente/maison/{slug}
    - meta_title / title_auto / details["fr"] (description)
    - photos (liste d'URLs img.netty.immo)
    - image_dpe (PNG du diagnostic → pas de lettre exploitable ⇒ dpe=None)

Particularités :
  - Réseau né à Strasbourg (Grand Est) ; sur les départements cibles l'inventaire
    est très faible et essentiellement composé d'annonces VENDUES (promo=1001),
    qu'on exclut. → souvent 0 bien EN VENTE dans la zone (scraper néanmoins valide).
  - Endpoint de résolution département (non utilisé ici, map statique) :
    POST /webapi/getData {data:{form:{keyword,type:["dpt"]}}}.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import base64
import json
import re

import httpx

from scrapers._base import HEADERS

BASE_URL = "https://www.immosurmesure.fr"


# Code département → slug d'URL Immosurmesure : /vente/maison/departement-{slug}
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

# Types de bien (champ prod_type Netty) à conserver : maisons / propriétés.
_KEEP_PROD_TYPE = {"house", "property", "propriete", "manor", "castle", "farm"}
# Tout le reste est exclu (appartement, terrain, immeuble, local, fonds, parking…).

_PROMO_VENDU = 1001  # promo == 1001 ⇒ annonce VENDUE (à exclure)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
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
                print(f"[Immosurmesure] Dept {dept}: {len(biens)} annonces en vente")
            except Exception as e:
                print(f"[Immosurmesure] Erreur dept {dept}: {e}")
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
    url = f"{BASE_URL}/vente/maison/departement-{slug}"
    r = await client.get(url)
    if r.status_code != 200:
        return []

    blob = _extract_prod_blob(r.text)
    if not blob:
        return []

    prods = blob.get("prodId", {}) or {}
    # `search` = résultats RÉELLEMENT filtrés par le département demandé ;
    # les autres groupes du blob sont des blocs « populaires » nationaux (fuite) → ignorés.
    refs = (blob.get("prodResults", {}) or {}).get("search", []) or []

    biens: list[dict] = []
    seen: set[str] = set()

    for ref in refs:
        p = prods.get(ref)
        if not p:
            continue
        try:
            bien = _parse_product(ref, p, dept)
        except Exception:
            continue
        if not bien:
            continue

        # Filtre département STRICT (sécurité même si `search` est censé être propre)
        if not bien["code_postal"] or bien["code_postal"][:2] != dept:
            continue

        if bien["id_annonce"] in seen:
            continue

        prix = bien.get("prix") or 0
        surf = bien.get("surface") or 0
        if prix_max and prix and prix > prix_max:
            continue
        if prix_min and prix and prix < prix_min:
            continue
        if surface_min and surf and surf < surface_min:
            continue

        seen.add(bien["id_annonce"])
        biens.append(bien)

    return biens


def _extract_prod_blob(html: str) -> dict | None:
    """Décode le blob base64 `JSON.parse(b64_to_utf8("..."))` qui contient prodId."""
    for m in re.finditer(r'b64_to_utf8\("([^"]+)"\)', html):
        try:
            dec = base64.b64decode(m.group(1)).decode("utf-8")
        except Exception:
            continue
        if dec.startswith('{"prodId"'):
            try:
                return json.loads(dec)
            except Exception:
                return None
    return None


def _parse_product(ref: str, p: dict, dept: str) -> dict | None:
    prod_type = (p.get("prod_type") or "").lower()
    if prod_type not in _KEEP_PROD_TYPE:
        return None

    # Exclure les annonces VENDUES
    if p.get("promo") == _PROMO_VENDU:
        return None

    prix = _extract_price(p)
    if prix is None:
        # pas de prix numérique (souvent = vendu/masqué) → on écarte
        return None

    cp = str(p.get("cp") or "").strip()
    ville = (p.get("city") or p.get("sector") or "").strip()

    surface = _to_float(p.get("surface")) or _to_float(p.get("surface_carrez"))
    surface_terrain = _to_float(p.get("land"))
    pieces = _to_int(p.get("rooms"))
    chambres = _to_int(p.get("rooms2"))

    titre = (
        _lang(p.get("title_auto"))
        or _lang(p.get("meta_title"))
        or f"Maison {ville} ({cp})"
    )
    description = _strip_html(_lang(p.get("details")) or _lang(p.get("meta_desc")) or "")

    slug = _lang(p.get("url"))
    if slug:
        url = f"{BASE_URL}/vente/maison/{slug}"
    else:
        url = f"{BASE_URL}/vente/maison/departement-{DEPT_SLUGS.get(dept, '')}"

    photos = [u for u in (p.get("photos") or []) if isinstance(u, str)][:12]

    return {
        "source": "immosurmesure",
        "url": url,
        "id_annonce": p.get("prod_ref") or ref,
        "titre": str(titre)[:150],
        "type_bien": "maison",
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
        "dpe": None,  # image_dpe est un PNG (pas de lettre exploitable sans OCR)
        "agence": "Immosurmesure",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _lang(field) -> str:
    """Champs Netty parfois sous forme {"fr": "..."} ; renvoie la valeur FR/str."""
    if isinstance(field, dict):
        return str(field.get("fr") or next(iter(field.values()), "") or "")
    if field is None:
        return ""
    return str(field)


def _extract_price(p: dict) -> float | None:
    """price = formated.price.amount (int si en vente, 'Vendu' sinon)."""
    amount = (((p.get("formated") or {}).get("price") or {}).get("amount"))
    val = _to_float(amount)
    if val and val > 1000:
        return val
    return None


def _to_float(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v else None
    s = re.sub(r"[^\d.]", "", str(v).replace(",", "."))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _to_int(v) -> int | None:
    f = _to_float(v)
    return int(f) if f else None


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


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
    print(f"\nTotal Immosurmesure: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b.get('pieces') or '?'}p — {b['ville']}"
        )

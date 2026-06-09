"""scrapers/immorevente.py — Immorevente (groupe d'agences Cher 18 + Indre 36)

Méthode : scrape_simple (httpx) — SSR, plateforme Netty (React).
           Le HTML de chaque page liste contient un gros blob JSON encodé en
           base64 (état SSR du front React). On le décode et on lit :
             - prodId       : dict {ref → objet bien complet}
             - prodResults["search"] : liste AUTORITAIRE des refs réellement
                                       retournées par la recherche (exclut les
                                       widgets « biens similaires/à la une » qui
                                       injectent sinon un bien hors-zone, ex 18390).
             - prodCount    : total serveur (paginé côté client au-delà de ~11).

URL pattern : /vente/{ville-slug}/{cp}        (ex: /vente/bourges/18000)
              Les URLs filtrées par ville renvoient en SSR la totalité des biens
              de la commune quand prodCount ≲ 11. La pagination profonde n'existe
              qu'en client-side (API api.netty.fr, non utilisée).

Filtre département : le site n'a PAS de filtre département serveur ; il filtre
              par ville+CP. Stratégie = énumérer les communes des départements
              cibles (DEPT_COMMUNES) puis POST-FILTRE STRICT cp[:2] == dept.
              → un bien « à la une » d'un autre CP est rejeté par le post-filtre.

Couverture réelle : le groupe est implanté dans le Cher (18) et marginalement
              l'Indre (36). Pour les autres départements (72/28/45/89...), aucune
              commune n'a de stock → la recherche renvoie 0 (vérifié, 0 fuite).

Type de bien : champ prod_type (house/appt/land/...). On ne garde que maisons /
              propriétés (pas d'appartements/terrains/commerces).

Détail : /vente/{url.fr},{prod_ref}

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import base64
import json
import re

import httpx

BASE_URL = "https://www.immorevente.fr"
MAX_COMMUNES = 60
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types prod_type Netty à conserver (maisons / propriétés / biens ruraux).
_KEEP_PROD_TYPE = {
    "house", "maison", "propriete", "property", "villa", "ferme", "farm",
    "manoir", "manor", "chateau", "castle", "longere", "moulin", "mas",
    "demeure", "domaine", "gite",
}
# Types explicitement écartés.
_EXCLUDE_PROD_TYPE = {
    "appt", "appartement", "apartment", "land", "terrain", "shop", "commerce",
    "local", "office", "bureau", "garage", "parking", "building", "immeuble",
    "fonds", "business", "car", "box",
}

# Communes (slug Netty → CP) par département cible.
# Filtre serveur = ville+CP ; le post-filtre cp[:2] garantit 0 fuite quel que
# soit le contenu réellement renvoyé. Les départements sans implantation (72,
# 28, 45, 89, …) n'ont volontairement pas de communes listées → 0 bien.
DEPT_COMMUNES: dict[str, list[tuple[str, str]]] = {
    # ── Cher (18) — cœur d'implantation du groupe ──
    "18": [
        ("bourges", "18000"),
        ("vierzon", "18100"),
        ("saint-doulchard", "18230"),
        ("trouy", "18570"),
        ("saint-florent-sur-cher", "18400"),
        ("mehun-sur-yevre", "18500"),
        ("aubigny-sur-nere", "18700"),
        ("saint-amand-montrond", "18200"),
        ("dun-sur-auron", "18130"),
        ("sancerre", "18300"),
        ("la-guerche-sur-l-aubois", "18150"),
        ("baugy", "18800"),
        ("avord", "18520"),
        ("levet", "18340"),
        ("le-chatelet", "18170"),
        ("lignieres", "18160"),
        ("nerondes", "18350"),
        ("henrichemont", "18250"),
        ("les-aix-d-angillon", "18220"),
        ("chateauneuf-sur-cher", "18190"),
        ("massay", "18120"),
        ("graçay", "18310"),
        ("foecy", "18500"),
        ("plaimpied-givaudins", "18340"),
        ("saint-germain-du-puy", "18390"),
    ],
    # ── Indre (36) — implantation marginale ──
    "36": [
        ("chateauroux", "36000"),
        ("issoudun", "36100"),
        ("deols", "36130"),
        ("le-blanc", "36300"),
        ("argenton-sur-creuse", "36200"),
        ("la-chatre", "36400"),
        ("buzancais", "36500"),
        ("levroux", "36110"),
        ("vatan", "36150"),
        ("valencay", "36600"),
        ("chabris", "36210"),
        ("aigurande", "36140"),
    ],
}


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
            communes = DEPT_COMMUNES.get(dept)
            if not communes:
                # Aucune implantation connue dans ce département → 0 bien.
                print(f"[Immorevente] Dept {dept}: pas d'implantation, 0 annonce")
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, communes[:MAX_COMMUNES],
                    prix_max, prix_min, surface_min,
                )
                results.extend(biens)
                print(f"[Immorevente] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Immorevente] Erreur dept {dept}: {e}")

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    communes: list[tuple[str, str]],
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_refs: set[str] = set()

    for ville_slug, cp in communes:
        url = f"{BASE_URL}/vente/{ville_slug}/{cp}"
        try:
            r = await client.get(url)
        except Exception as e:
            print(f"[Immorevente]   {ville_slug} ({cp}): erreur {e}")
            await asyncio.sleep(0.5)
            continue

        if r.status_code != 200:
            await asyncio.sleep(0.5)
            continue

        data = _extract_state(r.text)
        if not data:
            await asyncio.sleep(0.5)
            continue

        prod_id = data.get("prodId", {}) or {}
        # Refs réellement retournées par la recherche (exclut « à la une »).
        search_refs = (data.get("prodResults", {}) or {}).get("search", []) or []

        for ref in search_refs:
            obj = prod_id.get(ref)
            if not obj:
                continue

            bien = _parse_obj(obj, dept)
            if not bien:
                continue

            # ── POST-FILTRE DÉPARTEMENT STRICT (0 fuite) ──
            if not bien["code_postal"] or bien["code_postal"][:2] != dept:
                continue

            if bien["id_annonce"] in seen_refs:
                continue

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            seen_refs.add(bien["id_annonce"])
            biens.append(bien)

        await asyncio.sleep(0.5)

    return biens


# ── Extraction de l'état SSR base64 ───────────────────────────────────────────

def _extract_state(html: str) -> dict | None:
    """Décode le blob base64 du front React contenant prodId/prodResults."""
    for blob in re.findall(r"[A-Za-z0-9+/=]{200,}", html):
        try:
            decoded = base64.b64decode(blob).decode("utf-8", "replace")
        except Exception:
            continue
        if '"prodId"' not in decoded:
            continue
        try:
            return json.loads(decoded)
        except Exception:
            continue
    return None


def _parse_obj(obj: dict, dept: str) -> dict | None:
    prod_type = str(obj.get("prod_type", "")).lower()
    if prod_type in _EXCLUDE_PROD_TYPE:
        return None
    if prod_type and prod_type not in _KEEP_PROD_TYPE:
        # type inconnu → on écarte par prudence (focus maisons/propriétés)
        return None

    # Vente uniquement (transact 1 = vente sur Netty ; rent rempli = location).
    if obj.get("transact") not in (None, 1, "1"):
        return None

    ref = obj.get("prod_ref") or obj.get("ref")
    if not ref:
        return None

    cp = str(obj.get("cp") or "").strip()
    ville = str(obj.get("city") or "").strip()

    url_slug = _lang(obj.get("url"))
    if url_slug:
        url = f"{BASE_URL}/vente/{url_slug},{ref}"
    else:
        url = f"{BASE_URL}/vente"

    titre = obj.get("title_auto") or _lang(obj.get("title")) or ""
    if not titre:
        titre = f"{_type_fr(prod_type)} {ville}".strip()

    description = _lang(obj.get("meta_desc")) or ""

    prix = _to_number(obj.get("price2")) or _to_number(obj.get("price1"))
    surface = _to_number(obj.get("surface")) or _to_number(obj.get("surface_carrez"))
    pieces = _to_int(obj.get("rooms"))
    chambres = _to_int(obj.get("rooms2"))

    photos = []
    for ph in obj.get("photos", []) or []:
        if isinstance(ph, str) and ph.startswith("http"):
            photos.append(ph)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "immorevente",
        "url": url,
        "id_annonce": str(ref),
        "titre": titre[:150],
        "type_bien": _type_fr(prod_type),
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Immorevente",
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _lang(val) -> str:
    """Champs Netty multilingues : {'fr': '...', 'en': '...'} → fr."""
    if isinstance(val, dict):
        return str(val.get("fr") or next(iter(val.values()), "") or "").strip()
    if isinstance(val, str):
        return val.strip()
    return ""


_TYPE_FR = {
    "house": "maison",
    "villa": "villa",
    "propriete": "propriété",
    "property": "propriété",
    "ferme": "ferme",
    "farm": "ferme",
    "manoir": "manoir",
    "chateau": "château",
    "longere": "longère",
    "moulin": "moulin",
    "mas": "mas",
    "demeure": "demeure",
    "domaine": "domaine",
    "gite": "gîte",
}


def _type_fr(prod_type: str) -> str:
    return _TYPE_FR.get(prod_type, prod_type or "maison")


def _to_number(val) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) if val else None
    s = re.sub(r"[^\d.]", "", str(val).replace(",", "."))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _to_int(val) -> int | None:
    n = _to_number(val)
    return int(n) if n else None


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
    print(f"\nTotal Immorevente: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

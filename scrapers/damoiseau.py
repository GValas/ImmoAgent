"""scrapers/damoiseau.py — Damoiseau Immo (agence locale Le Mans / Sarthe)

Méthode : scrape_simple (httpx) — SSR HTML, inventaire embarqué en JS.
Site : https://www.damoiseau.immo  (agence familiale mono-implantation, Le Mans).

Particularité :
  La page /resultats?transac=vente est servie en SSR (httpx 200, Cloudflare
  passif). Le HTML brut NE contient PAS de cartes <article>, mais une variable
  JavaScript inline `var properties = [ {...}, ... ];` qui porte l'inventaire
  complet (pagination client-side, pas d'AJAX). On extrait ce tableau par
  comptage de crochets (hors chaînes), on corrige le pseudo-JSON (échappements
  `\\'` JS, valeurs en quotes simples pour `ville`) et on lit chaque record.

  Champs par record : id, titre, image, lien, prix ("PRIX : 76 200 €"),
  description (HTML), surface (HTML, <span class="surface">NN</span>),
  ville (HTML, <span class="ville">Nom</span>). AUCUN code postal n'est exposé,
  ni dans la liste ni dans la page détail (le seul CP structuré du détail est
  l'adresse de l'AGENCE — à ne pas utiliser).

Filtre département — STRICT, 0 fuite :
  L'agence liste aussi des biens HORS Sarthe (ex. une "Verrières" en plein
  Perche → Orne 61). Comme il n'y a pas de CP, on résout le NOM DE COMMUNE
  via l'API geo.api.gouv.fr : on précharge la liste des communes des
  départements cibles (un appel /departements/{d}/communes par dept, mis en
  cache) → lookup commune normalisée → (dept, code_postal). Un bien n'est
  conservé QUE si sa commune figure dans ce lookup des départements demandés.
  Toute commune hors-zone (ou ambiguë non présente dans la zone) est rejetée :
  aucune fuite possible.

URL : {BASE_URL}/resultats?transac=vente

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import html
import re
import unicodedata

import httpx

from scrapers._base import HEADERS
from scrapers._base import parse_price_digits as _parse_price

BASE_URL = "https://www.damoiseau.immo"
LISTING_URL = f"{BASE_URL}/resultats?transac=vente"
GEO_API = "https://geo.api.gouv.fr"
PHOTOS_PER_CARD = 1  # la liste n'expose qu'une vignette par bien


# Cache lookup commune → (dept, code_postal) pour les départements demandés.
_COMMUNES_CACHE: dict[str, tuple[str, str]] = {}
_COMMUNES_DEPTS_LOADED: set[str] = set()


def _norm(s: str) -> str:
    """Normalise un nom de commune : sans accents, minuscules, séparateurs unifiés."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower().replace("-", " ").replace("'", " ").replace("’", " ")
    return " ".join(s.split())


async def _load_communes(client: httpx.AsyncClient, departements: list[str]) -> None:
    """Précharge (une fois par dept) le lookup commune normalisée → (dept, cp)."""
    for dept in departements:
        if dept in _COMMUNES_DEPTS_LOADED:
            continue
        try:
            r = await client.get(
                f"{GEO_API}/departements/{dept}/communes",
                params={"fields": "nom,codesPostaux"},
                timeout=20,
            )
            if r.status_code != 200:
                print(f"[Damoiseau] geo.api.gouv.fr dept {dept}: {r.status_code}")
                continue
            for c in r.json():
                cps = c.get("codesPostaux") or []
                cp = cps[0] if cps else f"{dept}000"
                # premier dept gagnant en cas de doublon de nom (les deux sont en zone)
                _COMMUNES_CACHE.setdefault(_norm(c["nom"]), (dept, cp))
            _COMMUNES_DEPTS_LOADED.add(dept)
        except Exception as e:
            print(f"[Damoiseau] Erreur chargement communes dept {dept}: {e}")
        await asyncio.sleep(0.2)


def _extract_properties_block(html_text: str) -> str | None:
    """Isole le littéral JS `var properties = [ ... ];` par comptage de crochets."""
    i = html_text.find("var properties")
    if i == -1:
        return None
    i = html_text.find("[", i)
    if i == -1:
        return None
    depth = 0
    instr = False
    quote = ""
    esc = False
    for k in range(i, len(html_text)):
        c = html_text[k]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if instr:
            if c == quote:
                instr = False
            continue
        if c in ('"', "'"):
            instr = True
            quote = c
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return html_text[i : k + 1]
    return None


_FIELD_RE = {
    "id": re.compile(r'"id"\s*:\s*"([^"]*)"'),
    "titre": re.compile(r'"titre"\s*:\s*"([^"]*)"'),
    "image": re.compile(r'"image"\s*:\s*"([^"]*)"'),
    "lien": re.compile(r'"lien"\s*:\s*"([^"]*)"'),
    "prix": re.compile(r'"prix"\s*:\s*"([^"]*)"'),
    # description peut contenir des \' (échappement JS) → on capture large
    "description": re.compile(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"'),
    "surface": re.compile(r'"surface"\s*:\s*"((?:[^"\\]|\\.)*)"'),
    # ville est en quotes simples : 'ville': '<i ...><span class="ville">X</span>'
    "ville": re.compile(r'"ville"\s*:\s*\'((?:[^\'\\]|\\.)*)\''),
}


def _split_records(block: str) -> list[str]:
    """Découpe le tableau en records {...} (comptage d'accolades hors chaînes)."""
    records: list[str] = []
    depth = 0
    start = None
    instr = False
    quote = ""
    esc = False
    for k, c in enumerate(block):
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if instr:
            if c == quote:
                instr = False
            continue
        if c in ('"', "'"):
            instr = True
            quote = c
            continue
        if c == "{":
            if depth == 0:
                start = k
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                records.append(block[start : k + 1])
                start = None
    return records


def _field(rec: str, name: str) -> str:
    m = _FIELD_RE[name].search(rec)
    if not m:
        return ""
    val = m.group(1).replace("\\'", "'").replace('\\"', '"').replace("\\/", "/")
    return html.unescape(val)


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s)


def _parse_surface(surface_html: str) -> float | None:
    m = re.search(r'class=["\']surface["\']>\s*([\d\s\xa0.,]+)', surface_html)
    if not m:
        m = re.search(r"(\d[\d\s\xa0.,]*)", _strip_tags(surface_html))
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
    try:
        return float(val)
    except ValueError:
        return None


def _parse_ville(ville_html: str) -> str:
    m = re.search(r'class=["\']ville["\']>\s*([^<]+)<', ville_html)
    name = m.group(1) if m else _strip_tags(ville_html)
    return name.strip()


def _type_from_text(titre: str, lien: str) -> str:
    s = f"{titre} {lien}".lower()
    if "appartement" in s:
        return "appartement"
    if "terrain" in s:
        return "terrain"
    if "maison" in s:
        return "maison"
    if "immeuble" in s:
        return "immeuble"
    if "local" in s or "commerce" in s:
        return "local commercial"
    if "propri" in s:
        return "propriété"
    return "bien"


def _pieces_from_titre(titre: str) -> int | None:
    m = re.search(r"(\d+)\s*pi[eè]ce", titre, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _surface_hab_from_titre(titre: str) -> float | None:
    m = re.search(r"(\d[\d\s\xa0]*)\s*m²", titre)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 2000:
                return f
        except ValueError:
            pass
    return None


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    if not departements:
        return []

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        await _load_communes(client, departements)

        try:
            r = await client.get(LISTING_URL)
        except Exception as e:
            print(f"[Damoiseau] Erreur requête listing: {e}")
            return []
        if r.status_code != 200:
            print(f"[Damoiseau] Listing HTTP {r.status_code}")
            return []

        block = _extract_properties_block(r.text)
        if not block:
            print("[Damoiseau] Variable JS `properties` introuvable (structure changée ?)")
            return []

        records = _split_records(block)
        print(f"[Damoiseau] {len(records)} biens dans l'inventaire (toutes communes)")

        seen_ids: set[str] = set()
        rejets_hors_zone = 0

        for rec in records:
            try:
                bien = _parse_record(rec, departements)
            except Exception:
                continue
            if bien is None:
                rejets_hors_zone += 1
                continue

            aid = bien["id_annonce"]
            if aid in seen_ids:
                continue

            # Filtre dept STRICT (re-vérification) : 0 fuite
            if not bien["code_postal"] or bien["code_postal"][:2] not in departements:
                rejets_hors_zone += 1
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
            results.append(bien)

        # comptage par dept
        from collections import Counter

        par_dept = Counter(b["departement"] for b in results)
        print(
            f"[Damoiseau] Retenus en zone : {len(results)} "
            f"({dict(par_dept)}) — rejets hors-zone : {rejets_hors_zone}"
        )

    return results


def _parse_record(rec: str, departements: list[str]) -> dict | None:
    aid = _field(rec, "id")
    lien = _field(rec, "lien")
    if not lien:
        return None
    url = lien if lien.startswith("http") else f"{BASE_URL}/{lien.lstrip('/')}"

    ville_raw = _field(rec, "ville")
    ville = _parse_ville(ville_raw)
    if not ville:
        return None

    # Résolution commune → (dept, cp) via le lookup des départements cibles.
    hit = _COMMUNES_CACHE.get(_norm(ville))
    if not hit:
        return None  # commune hors zone (ou ambiguë non en zone) → rejet
    dept, code_postal = hit
    if dept not in departements:
        return None

    titre = _field(rec, "titre")
    description = _strip_tags(_field(rec, "description")).strip()
    type_bien = _type_from_text(titre, lien)
    prix = _parse_price(_field(rec, "prix"))

    surface_field = _parse_surface(_field(rec, "surface"))
    # Pour un terrain, la "surface" affichée est celle du terrain.
    if type_bien == "terrain":
        surface = None
        surface_terrain = surface_field
    else:
        surface = surface_field or _surface_hab_from_titre(titre)
        surface_terrain = None

    pieces = _pieces_from_titre(titre)

    photos = []
    img = _field(rec, "image")
    if img and "logo" not in img.lower():
        if img.startswith("./"):
            img = f"{BASE_URL}/{img[2:]}"
        elif img.startswith("/"):
            img = f"{BASE_URL}{img}"
        elif not img.startswith("http"):
            img = f"{BASE_URL}/{img}"
        photos.append(img)

    return {
        "source": "damoiseau",
        "url": url,
        "id_annonce": aid or url,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Damoiseau Immo",
    }


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
    print(f"\nTotal Damoiseau: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:15]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

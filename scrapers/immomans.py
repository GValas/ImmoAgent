"""scrapers/immomans.py — Immomans (agence immobilière, Le Mans / Sarthe)

Méthode : api_inoff (httpx) — SPA React "Netty" (netty.immo) côté client, MAIS le
catalogue produits complet est injecté côté serveur dans la page sous forme d'un
blob base64 (`JSON.parse(b64_to_utf8("..."))`). On le décode directement en httpx,
aucun Playwright nécessaire.

Pattern de listing :
  GET https://www.immomans.fr/vente?page=N
    → 3e blob base64 inline = {"prodId": {ref: {...}}, "prodCount": NN, ...}
    → pagination via ?page=N (≈12-13 biens/page), s'arrête quand 0 nouveau ref.
  type_offer=1 = vente, =2 = location  (on ne garde que la vente)
  prod_type : house / appt / build / land …  (on ne garde que les maisons/propriétés)

Filtre département :
  Agence MONO-IMPLANTATION (Le Mans). 100 % du stock est en Sarthe (72).
  Pas de filtre serveur par dept → POST-FILTRE par code_postal[:2] (cp dans le blob).
  Aucune fuite possible (toutes les annonces ont un cp 72xxx).

Champs du blob exploités :
  ref / prod_ref, title.fr, prod_type, cp, city, surface, land (terrain),
  pricePrimary (ou formated.price.amount), roomsList (→ pièces), details.fr,
  url.fr (slug), photos[], geo (slug "ville/cp" pour reconstruire l'URL).

Volume : ~20 maisons (sur ~30 ventes), tout en 72. Faible mais réel.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import base64
import json
import re

import httpx

BASE_URL = "https://www.immomans.fr"
LISTING_URL = f"{BASE_URL}/vente"
MAX_PAGES = 12          # plafond de sécurité (catalogue réel ~3 pages)
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# prod_type Netty → on ne garde que les maisons / propriétés (pas appt/immeuble/terrain)
_KEEP_PROD_TYPES = {"house"}
_EXCLUDE_PROD_TYPES = {"appt", "build", "land", "parking", "garage", "shop", "office",
                       "fonds", "local", "immeuble", "terrain", "premises"}

# Mots-clés du titre pour requalifier le type de bien (affichage)
_TYPE_MAP = [
    (re.compile(r"château|chateau", re.IGNORECASE), "château"),
    (re.compile(r"manoir", re.IGNORECASE), "manoir"),
    (re.compile(r"longère|longere", re.IGNORECASE), "longère"),
    (re.compile(r"propriété|propriete|demeure", re.IGNORECASE), "propriété"),
    (re.compile(r"moulin", re.IGNORECASE), "moulin"),
    (re.compile(r"ferme|corps de ferme", re.IGNORECASE), "ferme"),
    (re.compile(r"villa", re.IGNORECASE), "villa"),
    (re.compile(r"maison", re.IGNORECASE), "maison"),
]
_BLOB_RE = re.compile(r'JSON\.parse\(b64_to_utf8\("([^"]+)"\)\)')


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    prods = await _fetch_all_prods()

    results: list[dict] = []
    seen: set[str] = set()
    for ref, raw in prods.items():
        bien = _parse_prod(ref, raw)
        if not bien:
            continue

        # POST-FILTRE département via code_postal[:2]
        cp = bien.get("code_postal") or ""
        dept = cp[:2] if len(cp) >= 2 else ""
        if departements and dept not in departements:
            continue
        bien["departement"] = dept

        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        aid = bien.get("id_annonce") or bien.get("url")
        if aid in seen:
            continue
        seen.add(aid)
        results.append(bien)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[Immomans] Dept {dept}: {n} annonces")

    return results


async def _fetch_all_prods() -> dict:
    """Pagine /vente?page=N et fusionne les blobs prodId jusqu'à épuisement."""
    prods: dict[str, dict] = {}
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25) as client:
        for page in range(1, MAX_PAGES + 1):
            url = LISTING_URL if page == 1 else f"{LISTING_URL}?page={page}"
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    break
            except Exception as e:
                print(f"[Immomans] Erreur page {page}: {e}")
                break

            blob = _extract_prod_blob(r.text)
            if not blob:
                break
            page_prods = blob.get("prodId") or {}
            if not page_prods:
                break

            new = 0
            for ref, raw in page_prods.items():
                if ref not in prods:
                    prods[ref] = raw
                    new += 1

            # Plus aucun nouveau bien → dernière page atteinte
            if new == 0:
                break
            # On a déjà tout le catalogue annoncé
            count = blob.get("prodCount") or 0
            if count and len(prods) >= count:
                break

            await asyncio.sleep(0.4)

    return prods


def _extract_prod_blob(html: str) -> dict | None:
    """Décode le blob base64 inline contenant {"prodId": ...}."""
    for b64 in _BLOB_RE.findall(html):
        try:
            raw = base64.b64decode(b64).decode("utf-8", "replace")
        except Exception:
            continue
        if '"prodId"' not in raw[:80]:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, dict) and "prodId" in data:
            return data
    return None


def _parse_prod(ref: str, p: dict) -> dict | None:
    try:
        # Vente uniquement (type_offer 1 = vente, 2 = location)
        if p.get("type_offer") not in (1, "1", None):
            return None

        prod_type = (p.get("prod_type") or "").lower()
        if prod_type in _EXCLUDE_PROD_TYPES:
            return None
        if prod_type and prod_type not in _KEEP_PROD_TYPES:
            # type inconnu → on garde seulement s'il « ressemble » à une maison via le titre
            pass

        titre = _localized(p.get("title")) or ""

        # Exclusion explicite par titre (sécurité)
        if re.search(r"\bappartement\b|\bstudio\b|\bterrain\b|\bimmeuble\b|\bgarage\b|\bparking\b",
                     titre, re.IGNORECASE):
            return None
        # Si prod_type non-house et titre ne contient aucun mot maison/propriété → exclure
        if prod_type not in _KEEP_PROD_TYPES:
            if not re.search(r"maison|propri[ée]t[ée]|demeure|manoir|ch[âa]teau|longère|longere|"
                             r"moulin|ferme|villa", titre, re.IGNORECASE):
                return None

        cp = str(p.get("cp") or "").strip()
        m_cp = re.search(r"\d{5}", cp)
        code_postal = m_cp.group(0) if m_cp else ""
        ville = (p.get("city") or "").strip()
        # Normalisation casse villes en MAJUSCULES
        if ville.isupper():
            ville = ville.title()

        prix = _to_float(p.get("pricePrimary"))
        if prix is None:
            prix = _to_float(p.get("price2"))
        if prix is None:
            fmt = p.get("formated") or {}
            prix = _to_float((fmt.get("price") or {}).get("amount"))

        surface = _to_float(p.get("surface"))
        surface_terrain = _to_float(p.get("land"))

        # Pièces : depuis roomsList (nb d'entrées principales) ou titre "N pièces"
        pieces = None
        m_pc = re.search(r"(\d+)\s*pi[èe]ces?", titre, re.IGNORECASE)
        if m_pc:
            pieces = int(m_pc.group(1))
        if pieces is None:
            slug = _localized(p.get("url")) or ""
            m_pc2 = re.search(r"(\d+)-pieces", slug)
            if m_pc2:
                pieces = int(m_pc2.group(1))

        # Chambres : depuis roomsList (entrées "Chambre")
        chambres = None
        rooms = p.get("roomsList")
        if isinstance(rooms, list):
            ch = sum(1 for rm in rooms
                     if isinstance(rm, dict) and re.search(r"chambre", str(rm.get("detail", "")), re.IGNORECASE))
            if ch:
                chambres = ch

        # URL détail : /vente/{prod_type-fr}/{slug}  — on reconstruit via geo + slug
        slug = _localized(p.get("url")) or ""
        geo = p.get("geo") or ""           # "ville/cp"
        type_seg = {"house": "maison", "appt": "appartement", "build": "immeuble",
                    "land": "terrain"}.get(prod_type, "maison")
        if slug and geo:
            url = f"{BASE_URL}/{type_seg}/{geo}/{slug}"
        elif slug:
            url = f"{BASE_URL}/{type_seg}/{slug}"
        else:
            url = f"{BASE_URL}/vente"

        description = _strip_html(_localized(p.get("details")) or "")

        # type de bien (affichage)
        type_bien = "maison"
        for rx, label in _TYPE_MAP:
            if rx.search(titre):
                type_bien = label
                break

        photos = []
        for ph in (p.get("photos") or []):
            if isinstance(ph, str) and ph.startswith("http"):
                photos.append(ph)
        if not photos and isinstance(p.get("image"), str) and p["image"].startswith("http"):
            photos.append(p["image"])
        photos = photos[:PHOTOS_PER_CARD]

        if not titre:
            titre = f"{type_bien.title()} {ville}".strip()

        return {
            "source": "immomans",
            "url": url,
            "id_annonce": p.get("prod_ref") or ref,
            "titre": titre[:150],
            "type_bien": type_bien,
            "description": description[:1200],
            "departement": code_postal[:2],
            "ville": ville[:80],
            "code_postal": code_postal,
            "surface": surface,
            "surface_terrain": surface_terrain,
            "pieces": pieces,
            "chambres": chambres,
            "prix": prix,
            "dpe": None,
            "photos": photos,
            "agence": "Immomans",
        }
    except Exception:
        return None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _localized(val) -> str | None:
    """Champs Netty localisés : {"fr": "...", "en": "..."} → prend 'fr' sinon str."""
    if isinstance(val, dict):
        return val.get("fr") or next(iter(val.values()), None)
    if isinstance(val, str):
        return val
    return None


def _to_float(val) -> float | None:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return f if f > 0 else None
    s = re.sub(r"[^\d,.\-]", "", str(val)).replace(",", ".")
    if s.count(".") > 1:
        s = s.replace(".", "")
    try:
        f = float(s)
        return f if f > 0 else None
    except ValueError:
        return None


def _strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
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
    print(f"\nTotal Immomans (depts cibles): {len(biens)} annonces")
    depts = sorted({(b["code_postal"] or "")[:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b.get('pieces') or '?'}p"
            f" — {b['type_bien']} — {b['ville']}"
        )

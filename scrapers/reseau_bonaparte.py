"""scrapers/reseau_bonaparte.py — Réseau Bonaparte / Bonaparte Art de Vivre

Réseau national de mandataires (biens de prestige). Le domaine historique
www.reseau-bonaparte.com redirige (301) vers www.bonaparte-artdevivre.com mais
sert le MÊME site (Next.js + backend Payload CMS, photos via media.apimo.pro).

⚠️ Certificat TLS EXPIRÉ sur reseau-bonaparte.com (et sur le domaine cible) au
moment du test → WebFetch/httpx avec vérification échouent. On scrape donc en
`verify=False` (dernier recours assumé, vérifié 2026-06-10).

Méthode : scrape_simple (httpx) — SSR.
  La liste /fr/properties est rendue CÔTÉ SERVEUR à condition de passer le
  paramètre `cities=CP1,CP2,...` (codes postaux). Sans filtre, seule une
  sélection « featured » (~9 biens nationaux) s'affiche ; AUCUN filtre par
  numéro de département. Stratégie filtre département :
    1. On récupère une fois la liste exhaustive des communes françaises via
       l'endpoint JSON /api/loadCompletedCities (≈35 000 communes avec leur
       postalCode) et on en déduit, par département cible, la liste des codes
       postaux.
    2. Pour chaque département cible on requête
       /fr/properties?cities={CP,...}#properties → le serveur ne renvoie QUE les
       biens situés dans ces communes (vérifié : 0 fuite hors-zone).
    3. Post-filtre STRICT `code_postal[:2] == dept` par sécurité.

Cartes : a[href^="/fr/properties/"] (hors le lien générique "/fr/properties")
  - URL    : href → /fr/properties/{slug}
  - Réf    : suffixe numérique du slug (id_annonce)
  - Loc    : "Ville · CP"  (bloc texte en tête de carte)
  - Prix   : "1 950 000 €"
  - Détail : "6 Chambres · 265 m2 intérieur"  (chambres + surface habitable)
  - Photos : img src → /_next/image?url=<apimo.pro>  → URL décodée

L'API Payload /api/properties est restreinte (renvoie 0 doc en accès public) :
on ne peut PAS l'utiliser directement, d'où le rendu SSR par CP.

Couverture in-zone observée (2026-06-10) : 72=1, 28=2, 49=9, 37=1, 53=1
(45/89/36/18/58/41 = 0). Toutes les annonces vues ont un CP in-zone → 0 fuite.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import urllib.parse

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int

BASE_URL = "https://www.reseau-bonaparte.com"
CITIES_ENDPOINT = "/api/loadCompletedCities"
PHOTOS_PER_CARD = 10

# Départements cibles (sécurité ; la liste réelle vient de criteres)
TARGET_DEPTS = {"72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"}


_TYPE_FROM_TEXT = re.compile(
    r"manoir|ch[aâ]teau|propri[eé]t[eé]|maison\s+de\s+ma[iî]tre|longère|longere|"
    r"demeure|domaine|villa|moulin|ferme|mas|maison",
    re.IGNORECASE,
)


async def _load_cps_by_dept(client: httpx.AsyncClient, depts: list[str]) -> dict[str, list[str]]:
    """Récupère la liste des codes postaux par département cible via l'endpoint
    JSON des communes (utilisé pour bâtir le filtre `cities=` du SSR)."""
    by_dept: dict[str, set[str]] = {d: set() for d in depts}
    try:
        r = await client.get(BASE_URL + CITIES_ENDPOINT, headers={**HEADERS, "Accept": "application/json"})
        if r.status_code != 200:
            print(f"[Bonaparte] loadCompletedCities status {r.status_code}")
            return {d: [] for d in depts}
        cities = r.json().get("cities", [])
    except Exception as e:
        print(f"[Bonaparte] Erreur loadCompletedCities: {e}")
        return {d: [] for d in depts}

    for ci in cities:
        cp = str(ci.get("postalCode") or "")
        if len(cp) == 5 and cp[:2] in by_dept:
            by_dept[cp[:2]].add(cp)
    return {d: sorted(by_dept[d]) for d in depts}


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    # Certificat TLS expiré → verify=False (dernier recours, cf. docstring).
    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30, verify=False
    ) as client:
        cps_by_dept = await _load_cps_by_dept(client, departements)

        for dept in departements:
            cps = cps_by_dept.get(dept, [])
            if not cps:
                continue
            try:
                biens = await _scrape_dept(
                    client, dept, cps, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[Bonaparte] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[Bonaparte] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_dept(
    client: httpx.AsyncClient,
    dept: str,
    cps: list[str],
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    url = (
        f"{BASE_URL}/fr/properties?cities="
        + urllib.parse.quote(",".join(cps))
        + "#properties"
    )
    r = await client.get(url)
    if r.status_code != 200:
        return biens

    soup = BeautifulSoup(r.text, "html.parser")
    cards = [
        a
        for a in soup.select('a[href*="/fr/properties/"]')
        if not a.get("href", "").rstrip("/").endswith("/properties")
    ]

    for card in cards:
        try:
            bien = _parse_card(card, dept)
        except Exception:
            continue
        if not bien:
            continue

        # Filtre STRICT département (le serveur filtre déjà, on re-vérifie)
        if not bien["code_postal"] or bien["code_postal"][:2] != dept:
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
    href = card.get("href", "")
    if not href or "/fr/properties/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    slug = href.rstrip("/").split("/fr/properties/")[-1]
    # id_annonce = suffixe numérique du slug (réf interne), sinon slug
    m_id = re.search(r"(\d+)$", slug)
    id_annonce = m_id.group(1) if m_id else slug

    text = card.get_text(" ", strip=True)

    # Biens vendus / sous compromis : on les écarte
    if re.search(r"\b(vendu|sous\s+compromis|sous\s+offre|r[eé]serv[eé])\b", text, re.IGNORECASE):
        return None

    # Localisation : "Ville · CP"
    ville, code_postal = _parse_loc(text)

    # Prix : élément dédié court contenant '€' (évite de coller CP + prix)
    prix = _parse_price_el(card)
    if prix is None:
        prix = _parse_price(text, code_postal)

    # Chambres + surface : "6 Chambres · 265 m2 intérieur"
    chambres = _parse_int(r"(\d+)\s*Chambres?", text)
    surface = _parse_surface(text)

    # Type de bien depuis le slug (ou texte)
    type_bien = _guess_type(slug) or "maison"

    # Titre depuis le slug nettoyé
    titre = _titre_from_slug(slug, ville)

    # Photos (apimo.pro encodées dans /_next/image?url=...)
    photos = []
    for img in card.select("img"):
        src = img.get("src") or img.get("data-src") or ""
        real = _decode_next_image(src)
        if real and real not in photos:
            photos.append(real)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "reseau_bonaparte",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Réseau Bonaparte",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_loc(text: str) -> tuple[str, str]:
    """'Saint-Malo · 35400 1 950 000 € ...' → ('Saint-Malo', '35400')"""
    m = re.search(r"^(.*?)\s*[·•]\s*(\d{5})", text)
    if m:
        return m.group(1).strip(), m.group(2)
    cp = ""
    m_cp = re.search(r"\b(\d{5})\b", text)
    if m_cp:
        cp = m_cp.group(1)
    return "", cp


def _parse_price_el(card) -> float | None:
    """Prix depuis l'élément dédié court (« 299 000 € »), pour ne pas coller
    le code postal au montant comme le ferait un parse sur tout le texte."""
    best = None
    for el in card.find_all(["span", "p", "div"]):
        t = el.get_text(" ", strip=True)
        if "€" in t and len(t) <= 24:
            cleaned = re.sub(r"[^\d]", "", t.split("€")[0])
            if cleaned:
                try:
                    val = float(cleaned)
                    # garde le plus petit candidat plausible (le montant pur)
                    if val >= 1000 and (best is None or val < best):
                        best = val
                except ValueError:
                    pass
    return best


def _parse_price(text: str, code_postal: str = "") -> float | None:
    """Repli : prix depuis le texte global, en retirant d'abord le code postal
    pour éviter de le concaténer au montant."""
    cleaned_text = text
    if code_postal:
        # ne retire que la 1re occurrence du CP (préfixe localisation)
        cleaned_text = cleaned_text.replace(code_postal, "", 1)
    m = re.search(r"([\d][\d\s\xa0 \.]{3,})\s*€", cleaned_text)
    if not m:
        return None
    cleaned = re.sub(r"[^\d]", "", m.group(1))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'265 m2 intérieur' / '265 m² intérieur' → 265.0"""
    m = re.search(r"(\d[\d\s\xa0]*)\s*m2?²?\s*(?:int[eé]rieur)?", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 5000:
                return f
        except ValueError:
            pass
    return None


def _guess_type(slug: str) -> str | None:
    cleaned = slug.replace("-", " ")
    m = _TYPE_FROM_TEXT.search(cleaned)
    if m:
        return m.group(0).lower().replace("é", "e")
    return None


def _titre_from_slug(slug: str, ville: str) -> str:
    # retire le suffixe -xx-NNNN de référence
    base = re.sub(r"[-\s]+[a-z]{0,3}-?\d+$", "", slug)
    titre = re.sub(r"-+", " ", base).strip()
    if not titre:
        titre = ville
    return titre


def _decode_next_image(src: str) -> str | None:
    """/_next/image?url=https%3A%2F%2Fmedia.apimo.pro%2F... → URL réelle décodée."""
    if not src:
        return None
    if "/_next/image" in src and "url=" in src:
        q = urllib.parse.urlparse(src).query
        params = urllib.parse.parse_qs(q)
        if params.get("url"):
            return params["url"][0]
    if src.startswith("http"):
        return src
    if src.startswith("//"):
        return "https:" + src
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
    print(f"\nTotal Réseau Bonaparte: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('chambres') or '?'}ch"
            f" — {b['type_bien']} — {b['ville']}"
        )

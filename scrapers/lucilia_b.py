"""scrapers/lucilia_b.py — Lucilia B. Immobilier (immobilier de prestige à Tours, 37)

Méthode : scrape_simple (httpx) — site ASP SSR ancien (iso-8859-15, layout tables).
La page /recherche.html n'existe pas ; le listing se fait par des pages "univers"
   /rNN/immobilier-{Type}-tours.html
qui listent ~6 fiches chacune (petite agence, pas de pagination).

Pages de VENTE retenues (slug d'URL) :
   r37  immobilier-Maison-Vente-tours   (maisons en vente)
   r30  immobilier-Propriete-tours      (propriétés / maisons bourgeoises)
   r29  immobilier-Maison-tours         (maisons, dont des "VENDU")
   r41  immobilier-Maison-Tours-Centre  (maisons Tours centre)
   r42  immobilier-Immeuble-tours       (immeubles — exclus au parsing)
On EXCLUT r39/r40/r33 (locations) et on ne garde que les liens détail `…achat-…`.

Lien détail : https://…/b{ID}_u{NN}-achat-{Type}-{VILLE}.html
   → pas de prix ni surface dans la liste ; on ouvre chaque fiche détail :
       - Prix  : "Prix FAI : 1 279 200 €uros"
       - Réf   : "Réf : T3158"
       - Type+Ville : <h1 class="typebien_region">Maison TOURS PREBENDES</h1>
       - Surface : premier "NNN m²" du texte (≈ surface habitable)
   La VILLE est un quartier/commune de l'agglo de Tours (aucun code postal sur le site).

Filtre département : l'agence est 100 % Indre-et-Loire (37). On mappe la ville
   (commune réelle, pas le quartier) → code postal, et on POST-FILTRE par
   code_postal[:2]. Si 37 n'est pas dans les départements cibles → 0 résultat.
   (0 fuite : toutes les communes connues sont en 37 ; les inconnues prennent 37500.)

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import html as _html
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.lucilia-b-immobilier.fr"

# Pages "univers" de VENTE à balayer (rNN/slug)
LISTING_PAGES = [
    "r37/immobilier-Maison-Vente-tours",
    "r30/immobilier-Propriete-tours",
    "r29/immobilier-Maison-tours",
    "r41/immobilier-Maison-Tours-Centre-tours",
    "r42/immobilier-Immeuble-tours",
]

PHOTOS_PER_CARD = 8
MAX_DETAILS = 60  # plafond de sécurité (petite agence)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Commune (telle qu'écrite dans le slug / H1, normalisée) → code postal.
# Toute l'agglo de Tours est en Indre-et-Loire (37).
VILLE_CP = {
    "TOURS": "37000",
    "JOUE-LES-TOURS": "37300",
    "JOUE LES TOURS": "37300",
    "SAINT-CYR-SUR-LOIRE": "37540",
    "SAINT CYR SUR LOIRE": "37540",
    "SAINT-AVERTIN": "37550",
    "SAINT AVERTIN": "37550",
    "SAINT-PIERRE-DES-CORPS": "37700",
    "LA RICHE": "37520",
    "LA-RICHE": "37520",
    "CHAMBRAY-LES-TOURS": "37170",
    "FONDETTES": "37230",
    "LUYNES": "37230",
    "ROCHECORBON": "37210",
    "VOUVRAY": "37210",
    "MONTLOUIS-SUR-LOIRE": "37270",
    "AMBOISE": "37400",
    "NAZELLES-NEGRON": "37530",
    "BALLAN-MIRE": "37510",
    "CORMERY": "37320",
    "METTRAY": "37390",
    "MONNAIE": "37380",
    "LA-MEMBROLLE-SUR-CHOISILLE": "37390",
    "SAINT-ANTOINE-DU-ROCHER": "37360",
    "VERNOU-SUR-BRENNE": "37210",
}
# Par défaut, toute commune inconnue est rattachée à Tours/37 (l'agence est 37 pur).
DEFAULT_CP = "37000"

# Quartiers de Tours → on les ramène à la commune "TOURS"
_TOURS_QUARTIERS = re.compile(
    r"^TOURS\b", re.IGNORECASE
)

_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|manoir|ch[âa]teau|longere|longère|demeure|"
    r"domaine|bourgeoise|h[ôo]tel particulier",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|immeuble|terrain|local|commerce|garage|parking|bureau",
    re.IGNORECASE,
)

_DETAIL_RX = re.compile(
    r"https?://(?:www\.)?lucilia-b-immobilier\.fr/(b\d+_u\d+-([a-z]+)-[^\"']+?\.html)",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    # Court-circuit : agence 100 % Indre-et-Loire (37)
    if departements and "37" not in departements:
        print("[LuciliaB] 37 hors départements cibles → 0 résultat")
        return []

    results: list[dict] = []
    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        detail_paths = await _collect_detail_links(client)
        print(f"[LuciliaB] {len(detail_paths)} fiches détail (achat) à ouvrir")

        seen_bid: set[str] = set()
        seen_keys: set[str] = set()
        for path, type_slug in detail_paths[:MAX_DETAILS]:
            bid = path.split("_")[0]  # bXXXXXXXX
            if bid in seen_bid:
                continue
            seen_bid.add(bid)
            try:
                bien = await _parse_detail(client, path, type_slug)
            except Exception as e:
                print(f"[LuciliaB] Erreur fiche {bid}: {e}")
                bien = None
            if not bien:
                continue

            cp = bien.get("code_postal") or ""
            dept = cp[:2] if len(cp) >= 2 else ""
            if departements and dept not in departements:
                continue  # 0 fuite

            # Dédup inter-univers : la même propriété est listée sous plusieurs
            # rNN/uNN avec des bID différents → on dédup par réf (T-number) puis
            # par (ville, surface, type).
            ref = (bien.get("id_annonce") or "").strip()
            key = ref if ref and not ref.startswith(bid.lstrip("b")) else (
                f"{bien['ville']}|{bien.get('surface')}|{bien['type_bien']}"
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            results.append(bien)
            await asyncio.sleep(0.25)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for d, n in sorted(by_dept.items()):
        print(f"[LuciliaB] Dept {d}: {n} annonces")

    return results


async def _collect_detail_links(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    """Balaie les pages univers de vente, renvoie [(path_detail, type_slug)] (achat only)."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for page in LISTING_PAGES:
        url = f"{BASE_URL}/{page}.html"
        try:
            r = await client.get(url)
            if r.status_code != 200:
                continue
        except Exception as e:
            print(f"[LuciliaB] Erreur listing {page}: {e}")
            continue

        text = r.content.decode("iso-8859-15", "replace")
        for m in _DETAIL_RX.finditer(text):
            path = m.group(1)
            transaction = m.group(2).lower()  # achat / location
            if transaction != "achat":
                continue  # on exclut les locations
            bid = path.split("_")[0]
            if bid in seen:
                continue
            seen.add(bid)
            # type depuis le slug : b..._uNN-achat-{Type}-{VILLE}.html
            mt = re.search(r"-achat-([^-]+(?:-[^-]+)*?)-[A-ZÀ-Ÿ]", path)
            type_slug = ""
            mtype = re.search(r"-achat-(.+?)-[A-ZÉÈÀ]", path)
            if mtype:
                type_slug = mtype.group(1).replace("-", " ")
            out.append((path, type_slug))
        await asyncio.sleep(0.3)
    return out


async def _parse_detail(
    client: httpx.AsyncClient, path: str, type_slug: str
) -> dict | None:
    url = f"{BASE_URL}/{path}"
    r = await client.get(url)
    if r.status_code != 200:
        return None
    raw = r.content.decode("iso-8859-15", "replace")

    bid = path.split("_")[0].lstrip("b")

    # Type + ville depuis le H1 : "Maison TOURS PREBENDES" / "Propriété TOURS"
    h1 = re.search(r'typebien_region">(.*?)</h1>', raw, re.S)
    h1_txt = _clean(h1.group(1)) if h1 else ""
    type_bien, ville_raw = _split_type_ville(h1_txt, type_slug)

    if _EXCLUDE_TYPE.search(type_bien) and not _KEEP_TYPE.search(type_bien):
        return None
    if not _KEEP_TYPE.search(type_bien or type_slug):
        return None

    # Ville → commune → code postal (dept 37)
    ville, code_postal = _ville_to_cp(ville_raw)

    # Prix FAI
    prix = None
    mp = re.search(r"Prix\s*FAI[^0-9]*([\d][\d\s\xa0]{3,})\s*(?:&euro;|€)", raw, re.I)
    if mp:
        prix = _to_int(mp.group(1))

    # Référence
    ref = None
    mr = re.search(r"R&eacute;f\s*:\s*([A-Za-z0-9]+)", raw)
    if not mr:
        mr = re.search(r"R[ée]f\s*:\s*([A-Za-z0-9]+)", raw)
    if mr:
        ref = mr.group(1)

    # Surface habitable : premier "NNN m²" plausible du texte
    surface = None
    for ms in re.finditer(r"(\d[\d\s\xa0]{1,5})\s*m(?:&sup2;|&#178;|²|2)", raw):
        val = _to_int(ms.group(1))
        if val and 15 <= val <= 3000:
            surface = float(val)
            break

    # Terrain : "terrain d'environ NNN m²" / "terrain de N hectare"
    surface_terrain = _parse_terrain(raw)

    # Pièces / chambres depuis le texte
    pieces = _first_int(r"(\d+)\s*pi[èe]ce", raw)
    chambres = _first_int(r"(\d+)\s*chambre", raw)

    # Description : meta description ou premier paragraphe
    description = ""
    md = re.search(r'name="description"\s+content="([^"]*)"', raw, re.I)
    if md:
        description = _clean(md.group(1))

    # Photos (galerie /image/galerie/…)
    photos = []
    for mimg in re.finditer(r'(https?://[^"\']*?/image/galerie/[^"\']+?\.jpg[^"\']*)', raw, re.I):
        src = mimg.group(1)
        if src not in photos:
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    titre = h1_txt or f"{type_bien} {ville}".strip()

    return {
        "source": "lucilia_b",
        "url": url,
        "id_annonce": ref or bid,
        "titre": titre[:150],
        "type_bien": (type_bien or "maison").lower(),
        "description": description[:1200],
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": float(prix) if prix else None,
        "dpe": None,
        "photos": photos,
        "agence": "Lucilia B. Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _split_type_ville(h1_txt: str, type_slug: str) -> tuple[str, str]:
    """'Maison TOURS PREBENDES' → ('Maison', 'TOURS PREBENDES').
    Le type est le(s) premier(s) mot(s) avant la ville en MAJUSCULES."""
    if not h1_txt:
        return (type_slug.strip() or "maison"), ""
    # La ville commence au premier mot tout en majuscules
    parts = h1_txt.split()
    type_words, ville_words = [], []
    for w in parts:
        # un mot ville = MAJUSCULES (avec accents/traits d'union), longueur >1
        if ville_words or (w.isupper() and len(re.sub(r"[^A-ZÀ-Ÿ]", "", w)) >= 2):
            ville_words.append(w)
        else:
            type_words.append(w)
    type_bien = " ".join(type_words).strip() or type_slug.strip() or "maison"
    ville = " ".join(ville_words).strip()
    return type_bien, ville


def _ville_to_cp(ville_raw: str) -> tuple[str, str]:
    """'TOURS PREBENDES' → ('Tours', '37000') ; 'JOUE LES TOURS' → ('Joué-lès-Tours','37300')."""
    if not ville_raw:
        return "Tours", DEFAULT_CP
    norm = ville_raw.upper().strip()
    # quartier de Tours
    if _TOURS_QUARTIERS.match(norm):
        return ("Tours" if norm == "TOURS" else _titlecase(ville_raw)), VILLE_CP["TOURS"]
    # match direct (tirets ou espaces)
    key_space = norm
    key_dash = norm.replace(" ", "-")
    cp = VILLE_CP.get(key_space) or VILLE_CP.get(key_dash)
    if cp:
        return _titlecase(ville_raw), cp
    # commune inconnue de l'agglo → 37 par défaut
    return _titlecase(ville_raw), DEFAULT_CP


def _titlecase(s: str) -> str:
    return " ".join(p.capitalize() for p in s.split())


def _to_int(text: str) -> int | None:
    cleaned = re.sub(r"[^\d]", "", text.replace("\xa0", ""))
    try:
        return int(cleaned) if cleaned else None
    except ValueError:
        return None


def _first_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_terrain(raw: str) -> float | None:
    # hectares
    mh = re.search(r"terrain[^.]{0,30}?(\d+(?:[.,]\d+)?)\s*hectare", raw, re.I)
    if mh:
        try:
            return float(mh.group(1).replace(",", ".")) * 10000
        except ValueError:
            pass
    mt = re.search(r"terrain[^.]{0,30}?(\d[\d\s\xa0]{1,6})\s*m(?:&sup2;|&#178;|²|2)", raw, re.I)
    if mt:
        v = _to_int(mt.group(1))
        if v and 20 <= v <= 1_000_000:
            return float(v)
    return None


# ── CLI standalone ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    try:
        from config_loader import load_criteria

        criteres = load_criteria()
        depts = criteres.departements
        prix_max = criteres.prix_max
        prix_min = getattr(criteres, "prix_min", 0)
        surface_min = criteres.surface_min
    except Exception:
        depts = ["72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"]
        prix_max = prix_min = surface_min = 0

    biens = asyncio.run(
        search(
            {
                "departements": depts,
                "prix_max": prix_max,
                "prix_min": prix_min,
                "surface_min": surface_min,
            }
        )
    )
    print(f"\nTotal Lucilia B: {len(biens)} annonces")
    vus = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {vus}")
    by_dept: dict[str, int] = {}
    for b in biens:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print(f"Par dept : {by_dept}")
    for b in biens[:15]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€ — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}"
            f" — {b['type_bien']} — {b['ville']}"
        )

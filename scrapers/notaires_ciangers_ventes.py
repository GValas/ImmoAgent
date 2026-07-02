"""scrapers/notaires_ciangers_ventes.py — Chambre interdép. des Notaires du Grand Anjou

Méthode : scrape_simple (httpx) — SSR (Next.js / React Server Components)
Site    : https://www.ci-angers.notaires.fr  (ventes notariales)

Périmètre de l'office = Maine-et-Loire (49) + Mayenne (53) + Sarthe (72).
Tous trois sont des départements cibles, mais l'office liste aussi quelques biens
hors-zone (mandats partagés : Paris, etc.) → filtre département STRICT obligatoire.

Stratégie
---------
1. UNE seule requête sur /immobilier renvoie le **catalogue complet** dans le
   payload RSC streamé (`self.__next_f.push`). On parse le tableau `items` :
   chaque objet = {id, bien (type), title, num (réf), pieces, city, price_int}.
   → PAS de pagination serveur exploitable (le param ?page= est ignoré, tout est
     déjà dans la page) ; PAS de CP ni de département fiable dans la liste
     (`num`/`crpcen` = code de l'OFFICE notarial, pas du bien : un bien parisien
     porte le préfixe 72126 de l'étude du Mans).

2. Filtre liste : on ne garde que les types maison/propriété/... et les bornes
   de prix (price_int connu en liste).

3. Pour chaque survivant on visite la page détail /annonce/{id} (SSR) qui donne
   le **vrai** département du bien — bloc visible "Ville ( NN )" — ainsi que CP
   (objet ville `Code_Postal`/`cp` du bien), surface, terrain, pièces, description
   et galerie photos (/images/storage/files/annonce/.../{id}/...).

4. **Post-filtre STRICT** : on ne conserve le bien QUE si son département réel
   (issu de la page détail) ∈ départements cibles → 0 fuite garantie.

Limite : le détail est requêté par bien (catalogue ~2-3k annonces) ; on cape à
MAX_DETAILS candidats (déjà pré-filtrés type+prix) pour rester poli.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.ci-angers.notaires.fr"
LISTING_URL = f"{BASE_URL}/immobilier"
MAX_DETAILS = 80           # plafond de pages détail visitées par run
DETAIL_CONCURRENCY = 8
PHOTOS_PER_CARD = 12


# Types de bien (champ "bien" du payload) à conserver
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|gite|gîte|corps",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"bien_divers|viager",
    re.IGNORECASE,
)

# Item du catalogue dans le flux RSC (échappement \" = un backslash dans le HTML)
_ITEM_RE = re.compile(
    r'\{\\"id\\":(\d+),\\"type\\":\d+,'
    r'\\"bien\\":\\"([^"\\]+)\\",'
    r'\\"title\\":\\"((?:[^"\\]|\\.)*?)\\",'
    r'\\"num\\":\\"((?:[^"\\]|\\.)*?)\\".*?'
    r'\\"city\\":\\"((?:[^"\\]|\\.)*?)\\",'
    r'\\"price_int\\":(\d+)'
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
    ) as client:
        # 1. Catalogue complet en une requête
        try:
            r = await client.get(LISTING_URL)
        except Exception as e:
            print(f"[CiAngers] Erreur listing : {e}")
            return []
        if r.status_code != 200:
            print(f"[CiAngers] Listing status {r.status_code}")
            return []

        items = _parse_listing(r.text)
        print(f"[CiAngers] Catalogue : {len(items)} annonces brutes")

        # 2. Pré-filtre type + prix (le département n'est PAS fiable ici)
        candidats: list[dict] = []
        for it in items:
            bien_type = it["bien"]
            if _EXCLUDE_TYPE.search(bien_type) and not _KEEP_TYPE.search(bien_type):
                continue
            if not _KEEP_TYPE.search(bien_type):
                continue
            p = it["prix"]
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            candidats.append(it)

        if len(candidats) > MAX_DETAILS:
            print(
                f"[CiAngers] {len(candidats)} candidats > plafond "
                f"{MAX_DETAILS} → tronqué"
            )
            candidats = candidats[:MAX_DETAILS]
        print(f"[CiAngers] {len(candidats)} candidats après filtre type+prix")

        # 3. Page détail (vrai département) + post-filtre STRICT
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
        tasks = [
            _enrich_detail(client, sem, it, departements, surface_min)
            for it in candidats
        ]
        enriched = await asyncio.gather(*tasks)

    results = [b for b in enriched if b is not None]
    print(f"[CiAngers] {len(results)} annonces retenues (dept cible + surface)")
    return results


def _parse_listing(html: str) -> list[dict]:
    """Extrait le catalogue du payload RSC ; déduplique par id."""
    seen: dict[str, dict] = {}
    for m in _ITEM_RE.finditer(html):
        aid, bien, title, num, city, price = m.groups()
        if aid in seen:
            continue
        seen[aid] = {
            "id": aid,
            "bien": bien.strip(),
            "title": _unescape(title),
            "num": _unescape(num),
            "city": _unescape(city),
            "prix": int(price) if price else None,
        }
    return list(seen.values())


async def _enrich_detail(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    it: dict,
    departements: set[str],
    surface_min: int,
) -> dict | None:
    aid = it["id"]
    url = f"{BASE_URL}/annonce/{aid}"
    async with sem:
        try:
            r = await client.get(url)
        except Exception:
            return None
        await asyncio.sleep(0.2)
    if r.status_code != 200:
        return None

    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "svg", "path", "noscript"]):
        tag.decompose()
    txt = soup.get_text("\n", strip=True)

    # Département réel du bien : bloc visible "Ville ( NN )"  (source FIABLE)
    dept = _detail_dept(txt)

    # Filtre STRICT : on exige un département cible
    if dept is None:
        return None
    if dept not in departements:
        return None

    # CP du bien : on tente de mapper le nom de ville (item.city) sur la table
    # de villes embarquée dans la page. Précision town-level best-effort ; si le
    # CP trouvé n'a pas le bon préfixe dept, on le rejette (on garde le dept).
    code_postal = _detail_cp(html, it.get("city", ""), dept)

    surface = _detail_num(r"Surface\s*:\s*\n?(\d[\d\s ]*)\s*\n?m", txt)
    if surface_min and surface and surface < surface_min:
        return None

    terrain = _detail_num(
        r"Surface du terrain\s*:\s*\n?(\d[\d\s ]*)\s*\n?m", txt
    )
    pieces = _detail_int(r"Nombre pièces\s*:\s*\n?(\d+)", txt)
    if pieces is None and str(it.get("pieces", "")).isdigit():
        pieces = int(it["pieces"])

    ref = _detail_ref(txt) or it.get("num")
    description = _detail_description(txt)
    photos = _detail_photos(html, aid)
    dpe = _detail_dpe(txt)

    ville = _clean_city(it.get("city", ""))
    type_bien = it["bien"].replace("_", " ").strip() or "maison"
    titre = it.get("title") or f"{type_bien.title()} {ville}".strip()

    return {
        "source": "notaires_ciangers_ventes",
        "url": url,
        "id_annonce": ref or aid,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1500],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": terrain,
        "pieces": pieces,
        "chambres": None,
        "prix": float(it["prix"]) if it.get("prix") else None,
        "photos": photos,
        "dpe": dpe,
        "agence": "Notaires du Grand Anjou (CI Angers)",
    }


# ── Helpers détail ─────────────────────────────────────────────────────────────

def _detail_dept(txt: str) -> str | None:
    """Bloc visible 'Ville ( NN )' → département réel du bien (2 chiffres)."""
    m = re.search(r"\(\s*(\d{2,3})\s*\)", txt)
    if not m:
        return None
    code = m.group(1)
    # Paris/banlieue (75/92/93/94) restent sur 2 chiffres ; on tronque à 2
    return code[:2]


def _detail_cp(html: str, city: str, dept: str | None) -> str:
    """CP du bien : map le nom de ville (item.city) sur la table de villes
    embarquée dans la page (objets {ville_origine, Nom, Code_Postal}).

    Best-effort town-level : retourne "" si la ville n'est pas dans la table ou
    si le CP trouvé n'a pas le préfixe du département réel (on ne garde JAMAIS un
    CP incohérent avec le dept visible, qui lui fait foi pour le filtre).
    """
    if not city:
        return ""
    table: dict[str, str] = {}
    for orig, nom, cp in re.findall(
        r'ville_origine\\":\\"([^"]+)\\",\\"arrondissement\\":[^,]*,'
        r'\\"Nom\\":\\"([^"]+)\\"[^}]*?Code_Postal\\":\\"(\d{5})\\"',
        html,
    ):
        table[_norm(nom)] = cp
        table[_norm(orig)] = cp
    cp = table.get(_norm(city), "")
    if cp and dept and cp[:2] != dept:
        return ""
    return cp


def _norm(s: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _detail_num(pattern: str, txt: str) -> float | None:
    m = re.search(pattern, txt, re.IGNORECASE)
    if not m:
        return None
    val = re.sub(r"[\s ]", "", m.group(1))
    try:
        f = float(val)
        return f if f > 0 else None
    except ValueError:
        return None


def _detail_int(pattern: str, txt: str) -> int | None:
    m = re.search(pattern, txt, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _detail_ref(txt: str) -> str:
    m = re.search(r"Réf\.?\s*:?\s*\n?([0-9A-Za-z][0-9A-Za-z/\-]+)", txt)
    return m.group(1) if m else ""


def _detail_description(txt: str) -> str:
    m = re.search(r"Description\s*\n(.+?)(?:\n(?:Localisation|Contact|Réf\.|"
                  r"Mentions|Honoraires|Le prix)|\Z)", txt, re.IGNORECASE | re.DOTALL)
    if m:
        desc = re.sub(r"\n{2,}", "\n", m.group(1)).strip()
        return desc
    return ""


def _detail_dpe(txt: str) -> str | None:
    m = re.search(r"(?:DPE|Diagnostic)[^A-G]{0,40}\b([A-G])\b", txt)
    return m.group(1) if m else None


def _detail_photos(html: str, aid: str) -> list[str]:
    raw = re.findall(
        r"/images/storage/files/annonce/\d+/annonce/" + re.escape(aid) + r"/[^\"'\\\s]+",
        html,
    )
    seen, photos = set(), []
    for p in raw:
        p = p.rstrip("\\")
        if p in seen:
            continue
        seen.add(p)
        photos.append(BASE_URL + p if p.startswith("/") else p)
    return photos[:PHOTOS_PER_CARD]


def _clean_city(city: str) -> str:
    city = city.strip()
    if city.isupper():
        # "LA SUZE-SUR-SARTHE" → "La Suze-Sur-Sarthe"
        return city.title()
    return city


def _unescape(s: str) -> str:
    return (
        s.replace('\\"', '"')
        .replace("\\/", "/")
        .replace("\\u00e9", "é")
        .replace("\\n", " ")
        .strip()
    )


# ── CLI standalone ──────────────────────────────────────────────────────────────

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
    print(f"\nTotal CI Angers (Notaires Grand Anjou) : {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    cibles = {str(d).zfill(2) for d in criteres.departements}
    fuite = [d for d in depts if d not in cibles]
    print(f"Fuite hors-cible : {fuite if fuite else 'AUCUNE'}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}/{b['code_postal'] or '?'}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['pieces'] or '?'}p — {b['type_bien']} — {b['ville']}"
            f" — {len(b['photos'])} photos"
        )

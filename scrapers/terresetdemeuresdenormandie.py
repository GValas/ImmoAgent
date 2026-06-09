"""scrapers/terresetdemeuresdenormandie.py — Terres et Demeures de Normandie

Agence immobilière de caractère (Normandie : Eure 27, Calvados 14, parfois Orne 61).
Domaine DISTINCT de terresetdemeuresdefrance.com (déjà couvert par un autre scraper).

Méthode : scrape_simple (httpx) — SSR HTML (moteur Jalis).
URL pattern : pas de filtre département serveur. Les annonces sont regroupées par
              CATÉGORIE de bien, paginées par suffixe -w{N} :
                /{categorie}-w1, /{categorie}-w2, ...   (ex: /ancien-et-de-caractere-w2)
              On scrape toutes les catégories pertinentes puis on POST-FILTRE le
              département (CP[:2] ∈ cibles) — indispensable car le site mélange
              14 / 27 / 61 (Orne hors-zone du segment) sans filtre serveur.

Cartes : div.ann
  - URL/Titre : h2.txt_contenu > a[href, title]
                href = "details-...-{id}"  ; title = libellé complet avec CP entre ().
  - Réf       : "Réf. NNNN" dans le 1er bloc font-size-small
  - Prix      : .prix .no-wrap          → "1 099 000 €"
  - Desc      : div.txt_contenu (le bloc texte, hors h2)
  - Photos    : picture img[src/srcset] (media.jalis.pro/.../{small,medium,large}.webp)

Localisation : il n'existe AUCUN champ ville/CP structuré (ni en liste ni en détail —
  la page détail n'expose que l'adresse des agences). On déduit donc le département :
    1. code postal 5 chiffres dans le titre  → dept = CP[:2]   (prioritaire, strict)
    2. à défaut, nom de département dans titre+desc (eure→27, calvados→14, orne→61)
  Tout bien dont AUCUN signal n'est dans la zone cible est écarté (0 fuite).
  Les CP/dept hors-zone (ex. 61 Orne) sont rejetés même si un autre signal matche.

Type de bien : déduit de la catégorie + mots-clés du titre. On garde maisons /
               propriétés / châteaux / fermes / haras ; on exclut les purs terrains
               et les gîtes/chambres d'hôtes.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.terresetdemeuresdenormandie.com"
MAX_PAGES = 10
PHOTOS_PER_CARD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Catégories listées (pagination -w{N}). On scrape l'ensemble puis on post-filtre.
CATEGORIES = [
    "proprietes-et-chateaux",
    "ancien-et-de-caractere",
    "haras-et-fermes",
    "maisons-de-ville-et-appartements",
    "recent-et-contemporain",
    "terrains-et-biens-a-restaurer",
]

# Noms de département → code (repli quand aucun CP 5 chiffres dans le titre).
DEPT_NAMES: dict[str, str] = {
    "eure-et-loir": "28",   # à matcher AVANT "eure" (sinon faux positif)
    "eure": "27",
    "calvados": "14",
    "orne": "61",
    "seine-maritime": "76",
    "manche": "50",
}

# Type de bien : on conserve maisons / propriétés / châteaux / fermes / haras.
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|haras|corps de ferme|pressoir|"
    r"maison de ma[iî]tre|maison de caract[èe]re|maison normande",
    re.IGNORECASE,
)
# Catégories/biens à exclure d'office.
_EXCLUDE_CAT = {"gites-et-chambres-d-hote"}


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    target = set(departements)
    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for cat in CATEGORIES:
            if cat in _EXCLUDE_CAT:
                continue
            try:
                biens = await _scrape_categorie(
                    client, cat, target, seen_ids, prix_max, prix_min, surface_min
                )
                results.extend(biens)
                print(f"[TDNormandie] Catégorie {cat}: {len(biens)} annonces en zone")
            except Exception as e:
                print(f"[TDNormandie] Erreur catégorie {cat}: {e}")
            await asyncio.sleep(0.6)

    return results


async def _scrape_categorie(
    client: httpx.AsyncClient,
    cat: str,
    target: set[str],
    seen_ids: set[str],
    prix_max: int,
    prix_min: int,
    surface_min: int,
) -> list[dict]:
    biens: list[dict] = []

    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}/{cat}-w{page}"
        r = await client.get(url)
        if r.status_code != 200:
            break

        cards = BeautifulSoup(r.text, "html.parser").select("div.ann")
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            try:
                bien = _parse_card(card, cat, target)
            except Exception:
                continue
            if not bien:
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

        # Le site renvoie 200 + cartes même au-delà de la dernière page (boucle) :
        # on s'arrête quand plus aucune carte NOUVELLE n'apparaît.
        if new_on_page == 0:
            break

        await asyncio.sleep(0.5)

    return biens


def _parse_card(card, cat: str, target: set[str]) -> dict | None:
    link = card.select_one("h2.txt_contenu a")
    if not link:
        return None
    href = link.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

    titre = (link.get("title") or link.get_text(" ", strip=True)).strip()

    # Description : le bloc div.txt_contenu (hors h2)
    desc_el = None
    for d in card.select("div.txt_contenu"):
        desc_el = d
        break
    description = desc_el.get_text(" ", strip=True) if desc_el else ""

    blob = f"{titre} {description}"

    # ── Localisation / département ────────────────────────────────────────────
    code_postal, dept = _resolve_dept(blob, target)
    if dept is None:
        return None  # aucun signal en zone → écarté (0 fuite)

    # ── Type de bien ──────────────────────────────────────────────────────────
    type_bien = _resolve_type(cat, titre)
    if type_bien is None:
        return None

    # ── Réf (id_annonce) ──────────────────────────────────────────────────────
    ref = ""
    m_ref = re.search(r"R[ée]f\.?\s*([0-9]{2,})", card.get_text(" ", strip=True))
    if m_ref:
        ref = m_ref.group(1)
    id_slug = ""
    m_id = re.search(r"-(\d+)$", href)
    if m_id:
        id_slug = m_id.group(1)
    id_annonce = ref or id_slug or url

    # ── Prix ──────────────────────────────────────────────────────────────────
    prix_el = card.select_one(".prix .no-wrap") or card.select_one(".prix")
    prix = _parse_price(prix_el.get_text(" ", strip=True) if prix_el else "")

    # ── Surface / terrain / chambres (depuis le texte) ───────────────────────
    surface = _parse_surface_hab(blob)
    surface_terrain = _parse_terrain(blob)
    chambres = _parse_int(r"(\d+)\s*chambres?", blob)
    pieces = _parse_int(r"(\d+)\s*pi[èe]ces?", blob)

    ville = _parse_ville(titre)

    # ── Photos ────────────────────────────────────────────────────────────────
    photos = []
    for img in card.select("picture img, img"):
        src = img.get("src") or ""
        if not src:
            srcset = img.get("srcset") or ""
            if srcset:
                src = srcset.split(",")[0].strip().split(" ")[0]
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "terresetdemeuresdenormandie",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": (ville or "")[:80] or None,
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Terres et Demeures de Normandie",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_dept(blob: str, target: set[str]) -> tuple[str | None, str | None]:
    """Renvoie (code_postal, departement) si le bien est en zone cible, sinon (None, None).

    Stratégie stricte anti-fuite :
      1. CP 5 chiffres → dept = CP[:2]. Si un CP hors-zone apparaît, le bien est
         rejeté (sauf si un CP en-zone est aussi présent, cas des biens "entre
         ville A (zone) et ville B").
      2. À défaut de CP : nom de département (eure/calvados/orne...). Un nom
         hors-zone fait rejeter le bien ; un nom en-zone l'accepte.
    """
    low = blob.lower()

    # 1. Codes postaux explicites
    cps = re.findall(r"\b(\d{5})\b", blob)
    if cps:
        in_zone = [c for c in cps if c[:2] in target]
        if in_zone:
            # privilégie le 1er CP en zone
            cp = in_zone[0]
            return cp, cp[:2]
        # des CP, mais aucun en zone → hors-zone
        return None, None

    # 2. Repli par nom de département (ordre : entrées longues d'abord)
    found_in = None
    found_out = False
    for name, code in DEPT_NAMES.items():
        pattern = r"\b" + re.escape(name).replace(r"\-", "[ -]") + r"\b"
        if re.search(pattern, low):
            if code in target:
                if found_in is None:
                    found_in = code
            else:
                found_out = True
            # "eure-et-loir" matche aussi "eure" : on retire pour éviter double compte
            low = re.sub(pattern, " ", low)

    if found_in is not None:
        return None, found_in
    if found_out:
        return None, None
    return None, None


def _resolve_type(cat: str, titre: str) -> str | None:
    cat_label = cat.replace("-", " ")
    if "terrain" in cat.lower():
        # catégorie "terrains et biens à restaurer" : ne garder que si bâti évoqué
        if _KEEP_TYPE.search(titre):
            return _type_from_title(titre) or "maison à restaurer"
        return None
    if _KEEP_TYPE.search(titre):
        return _type_from_title(titre) or cat_label
    # type non identifié dans le titre mais catégorie bâtie → garder via catégorie
    if cat in ("proprietes-et-chateaux", "ancien-et-de-caractere",
               "haras-et-fermes", "recent-et-contemporain",
               "maisons-de-ville-et-appartements"):
        return cat_label
    return None


def _type_from_title(titre: str) -> str | None:
    m = _KEEP_TYPE.search(titre)
    if m:
        return m.group(0).lower()
    return None


def _parse_ville(titre: str) -> str | None:
    """Tente d'extraire une ville : mot(s) en majuscule juste avant un CP entre ()."""
    m = re.search(r"([A-ZÀ-ÿ][\wÀ-ÿ'’\-]+(?:[ '’-][A-ZÀ-ÿ][\wÀ-ÿ'’\-]+)*)\s*\((\d{5})\)", titre)
    if m:
        return m.group(1).strip()
    return None


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_terrain(text: str) -> float | None:
    """Gère 'terrain de 2 321 m²' et les hectares 'X ha YY a'."""
    # hectares + ares
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:ha|hectares?)\s*(\d+)?\s*(?:a\b|ares?)?", text, re.IGNORECASE)
    if m:
        ha = float(m.group(1).replace(",", "."))
        ares = float(m.group(2)) if m.group(2) else 0.0
        val = ha * 10000 + ares * 100
        if 50 <= val <= 5_000_000:
            return val
    # m² explicite
    m = re.search(r"terrain[^0-9]*?([\d\s\xa0]+)\s*m", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 50 <= f <= 5_000_000:
                return f
        except ValueError:
            pass
    return None


def _parse_surface_hab(text: str) -> float | None:
    if not text:
        return None
    m = re.search(
        r"(\d[\d\s\xa0]*)\s*m²?\s*(?:hab|habitable|de surface habitable)",
        text, re.IGNORECASE,
    )
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 2000:
                return f
        except ValueError:
            pass
    return None


# ── CLI standalone ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    crit = {
        "departements": criteres.departements,
        "prix_max": criteres.prix_max,
        "prix_min": getattr(criteres, "prix_min", 0),
        "surface_min": criteres.surface_min,
    }
    biens = asyncio.run(search(crit))
    print(f"\nTotal TD Normandie (départements criteria.md): {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    depts_named = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus (par CP) : {depts}")
    print(f"Départements vus (assignés) : {depts_named}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}/{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€ — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m² — {b['type_bien']}"
        )

    # Smoke test sur la zone réelle du site (14/27) — prouve le parsing.
    print("\n--- Smoke test zone réelle 27/14 (hors criteria.md) ---")
    smoke = asyncio.run(search({**crit, "departements": ["27", "14"]}))
    sd = sorted({b["departement"] for b in smoke if b["departement"]})
    sc = sorted({b["code_postal"][:2] for b in smoke if b["code_postal"]})
    print(f"Total: {len(smoke)} | depts assignés: {sd} | depts CP: {sc}")
    leak = [b for b in smoke if b["departement"] not in {"27", "14"}
            or (b["code_postal"] and b["code_postal"][:2] not in {"27", "14"})]
    print(f"Fuite hors 27/14 : {len(leak)}")
    for b in smoke[:8]:
        print(f"  [{b['departement']}/{b['code_postal']}] {b['titre'][:60]} — {b['prix']}€")

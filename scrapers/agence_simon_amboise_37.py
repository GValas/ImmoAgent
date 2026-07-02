"""scrapers/agence_simon_amboise_37.py — Agence Simon (Amboise, Indre-et-Loire 37)

Méthode : scrape_simple (httpx) — SSR HTML (CMS Immo-Facile / WordPress).

Site : https://agence-simon.com  (agence locale, 13-15 rue J.-J. Rousseau,
       37400 Amboise ; bureaux Amboise / Bléré / St-Cyr-sur-Loire / Vouvray /
       Rochecorbon). Agence MONO-DÉPARTEMENT : tout son stock est en
       Indre-et-Loire (37), au cœur du Val de Loire / Touraine.

URL catalogue (toutes les annonces, une seule page SSR, pas de pagination) :
    https://agence-simon.com/vente/?all=1
Le HTML brut contient déjà toutes les cartes (vérifié : ~190 cartes en SSR).
La page DÉTAIL (/details-bien/?id_offre=XXX) est en revanche rendue en JS et
n'expose ni titre ni code postal exploitables → on s'appuie uniquement sur la
vue liste.

Cartes : div.cesis_col-lg-4.mosaique-lebien
  - Prix   : .prix_du_bien_liste            → "139 000 €"
  - Photos : .photo_principale_du_bien_liste a[href]  (media.immo-facile.com)
  - Titre  : .titre_du_bien_liste a          (+ href ?id_offre=ID)
  - Pictos : .picto_du_bien_liste span       (img[title] + valeur) :
             "surface habitable" / "nombre de pièces" / "nombre de chambres" /
             "nombre de salle de bain"
  - Descr  : .descr_du_bien_liste

Stratégie filtre département (0 FUITE garantie) :
  Aucun code postal n'est présent dans le HTML (ni liste, ni détail). On extrait
  la commune depuis le TITRE (puis la description en repli) en la confrontant à
  une whitelist de communes d'Indre-et-Loire (37). Un bien n'est conservé QUE
  si une commune 37 connue est reconnue ; son code_postal est alors fixé à
  partir de cette commune (toujours 37xxx). Conséquence : le post-filtre
  code_postal[:2] == "37" passe toujours, et aucun autre département ne peut
  apparaître. Les communes ambiguës / hors-37 (ex. Montrichard en 41) ne sont
  pas dans la map → le bien est écarté par prudence (pas de fuite).

Le scraper ne s'active que si 37 fait partie des départements demandés.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import unicodedata

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://agence-simon.com"
CATALOG_URL = f"{BASE_URL}/vente/?all=1"
PHOTOS_PER_CARD = 10


# Whitelist communes d'Indre-et-Loire (37) → code postal.
# Couvre le secteur réellement travaillé par l'agence (Amboise, vallée de la
# Loire entre Tours et Amboise, coteaux de Vouvray, Bléré…) + un large filet de
# communes 37 voisines. Toute valeur ici est garantie 37xxx → pas de fuite.
COMMUNES_37: dict[str, str] = {
    "amboise": "37400",
    "tours": "37000",
    "saint-cyr-sur-loire": "37540",
    "vouvray": "37210",
    "rochecorbon": "37210",
    "vernou-sur-brenne": "37210",
    "vernou": "37210",
    "noizay": "37210",
    "chancay": "37210",
    "reugny": "37380",
    "montlouis-sur-loire": "37270",
    "veretz": "37270",
    "azay-sur-cher": "37270",
    "la-ville-aux-dames": "37700",
    "saint-pierre-des-corps": "37700",
    "saint-avertin": "37550",
    "joue-les-tours": "37300",
    "chambray-les-tours": "37170",
    "fondettes": "37230",
    "la-riche": "37520",
    "saint-genouph": "37510",
    "savonnieres": "37510",
    "ballan-mire": "37510",
    "parcay-meslay": "37210",
    "notre-dame-d-oe": "37390",
    "monnaie": "37380",
    "nazelles-negron": "37530",
    "pocé-sur-cisse": "37530",
    "poce-sur-cisse": "37530",
    "limeray": "37530",
    "cangey": "37530",
    "saint-ouen-les-vignes": "37530",
    "lussault-sur-loire": "37400",
    "mosnes": "37530",
    "souvigny-de-touraine": "37530",
    "chargé": "37530",
    "charge": "37530",
    "civray-de-touraine": "37150",
    "chenonceaux": "37150",
    "bléré": "37150",
    "blere": "37150",
    "la-croix-en-touraine": "37150",
    "saint-martin-le-beau": "37270",
    "dierre": "37150",
    "athée-sur-cher": "37270",
    "athee-sur-cher": "37270",
    "francueil": "37150",
    "saint-christophe-sur-le-nais": "37370",
    "neuille-pont-pierre": "37360",
    "chateau-renault": "37110",
    "château-renault": "37110",
    "autreche": "37110",
    "villedomer": "37110",
    "auzouer-en-touraine": "37110",
    "montreuil-en-touraine": "37530",
    "neuvy-le-roi": "37370",
    "luynes": "37230",
    "vallieres-les-grandes": "37150",
}

# Normalisation des titres : variantes fréquentes → clé de la map ci-dessus.
ALIASES_37: dict[str, str] = {
    "st cyr": "saint-cyr-sur-loire",
    "st cyr sur loire": "saint-cyr-sur-loire",
    "saint cyr sur loire": "saint-cyr-sur-loire",
    "st cyr-sur-loire": "saint-cyr-sur-loire",
    "montlouis sur loire": "montlouis-sur-loire",
    "montlouis": "montlouis-sur-loire",
    "st avertin": "saint-avertin",
    "saint avertin": "saint-avertin",
    "civray de touraine": "civray-de-touraine",
    "azay sur cher": "azay-sur-cher",
    "parcay meslay": "parcay-meslay",
    "parcay-meslay": "parcay-meslay",
    "poce sur cisse": "poce-sur-cisse",
    "pocé sur cisse": "poce-sur-cisse",
    "notre-dame-d-oe": "notre-dame-d-oe",
    "notre dame d oe": "notre-dame-d-oe",
    "saint christophe sur le nais": "saint-christophe-sur-le-nais",
    "vernou sur brenne": "vernou-sur-brenne",
    "chateau renault": "chateau-renault",
}

# Types de bien à conserver (maisons / propriétés / fermes…) et à exclure.
_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|mas|g[iî]te|troglodyt|bourgeoise|habitation|architecte|"
    r"caract[eè]re",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|terrain|cave|fonds\s+de\s+commerce|local|garage|"
    r"parking|immeuble|bureau|commerce|r[eé]sidence|programme",
    re.IGNORECASE,
)


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def _norm(s: str) -> str:
    """Minuscule, sans accents, espaces compactés."""
    s = _strip_accents(s.lower())
    s = re.sub(r"[\s ]+", " ", s).strip()
    return s


# Pré-normalisation des clés de la map pour la recherche par sous-chaîne.
_COMMUNE_LOOKUP: dict[str, str] = {}
for _name, _cp in COMMUNES_37.items():
    _COMMUNE_LOOKUP[_norm(_name.replace("-", " "))] = _cp
for _alias, _target in ALIASES_37.items():
    _cp = COMMUNES_37.get(_target)
    if _cp:
        _COMMUNE_LOOKUP[_norm(_alias)] = _cp

# On teste d'abord les noms les plus longs (évite qu'un préfixe court masque une
# commune plus spécifique).
_COMMUNE_KEYS = sorted(_COMMUNE_LOOKUP.keys(), key=len, reverse=True)


def _match_commune_37(*texts: str) -> tuple[str, str] | None:
    """Renvoie (ville, code_postal) si une commune 37 connue est reconnue."""
    blob = " " + _norm(" ".join(t for t in texts if t)) + " "
    for key in _COMMUNE_KEYS:
        # frontière de mot pour éviter les faux positifs (ex. 'tours' dans 'séjours')
        if re.search(r"(?<![a-z])" + re.escape(key) + r"(?![a-z])", blob):
            cp = _COMMUNE_LOOKUP[key]
            ville = key.title()
            return ville, cp
    return None


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    if "37" not in departements:
        # Agence exclusivement en Indre-et-Loire : rien à faire hors 37.
        return []

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
    ) as client:
        try:
            r = await client.get(CATALOG_URL)
        except Exception as e:
            print(f"[AgenceSimon37] Erreur réseau: {e}")
            return []

        if r.status_code != 200:
            print(f"[AgenceSimon37] HTTP {r.status_code} sur {CATALOG_URL}")
            return []

        cards = BeautifulSoup(r.text, "html.parser").select(
            "div.cesis_col-lg-4.mosaique-lebien"
        )
        print(f"[AgenceSimon37] {len(cards)} cartes brutes")

        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue

            # Filtre département STRICT : code_postal toujours issu de la map 37.
            if not bien["code_postal"] or bien["code_postal"][:2] != "37":
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
            results.append(bien)

        await asyncio.sleep(0.3)

    print(f"[AgenceSimon37] {len(results)} annonces retenues (37)")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one(".titre_du_bien_liste a")
    if not link:
        return None
    href = link.get("href", "")
    if "id_offre=" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + "/" + href.lstrip("/")

    m_id = re.search(r"id_offre=([^&]+)", href)
    id_annonce = m_id.group(1) if m_id else url

    titre = link.get_text(" ", strip=True)

    # Description
    descr_el = card.select_one(".descr_du_bien_liste")
    description = descr_el.get_text(" ", strip=True) if descr_el else ""

    # Type de bien (titre + description). On exclut appart/terrain/commerce…
    type_blob = f"{titre} {description}"
    if _EXCLUDE_TYPE.search(type_blob) and not _KEEP_TYPE.search(type_blob):
        return None
    if not _KEEP_TYPE.search(type_blob):
        # type ambigu (souvent un appartement/terrain sans mot maison) → on écarte
        return None
    type_bien = "maison"

    # Localisation : commune 37 reconnue dans titre puis description.
    loc = _match_commune_37(titre) or _match_commune_37(description)
    if not loc:
        return None
    ville, code_postal = loc

    # Prix
    price_el = card.select_one(".prix_du_bien_liste")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Pictos : surface habitable / pièces / chambres
    surface = pieces = chambres = None
    for span in card.select(".picto_du_bien_liste span"):
        img = span.find("img")
        if not img:
            continue
        title = (img.get("title") or "").lower()
        val = span.get_text(" ", strip=True)
        if "surface habitable" in title:
            surface = _parse_surface(val)
        elif "nombre de pieces" in _strip_accents(title) or "nombre de pièces" in title:
            pieces = _parse_first_int(val)
        elif "chambre" in title:
            chambres = _parse_first_int(val)

    if not titre:
        titre = f"Maison {ville}"

    # Photos (galerie principale de la carte ; déduplication des URLs)
    photos: list[str] = []
    for a in card.select(".photo_principale_du_bien_liste a[href]"):
        src = a.get("href", "")
        if src.startswith("http") and src not in photos:
            photos.append(src)
    if not photos:
        for img in card.select(".photo_principale_du_bien_liste img"):
            src = img.get("src", "")
            if src.startswith("http") and src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "agence_simon_amboise_37",
        "url": url,
        "id_annonce": str(id_annonce),
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": "37",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Agence Simon (Amboise)",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text.replace(" ", ""))
    try:
        v = float(cleaned) if cleaned else None
    except ValueError:
        return None
    # garde-fou : ignore les valeurs aberrantes (ex. "0 €")
    return v if v and v > 1000 else None


def _parse_surface(text: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)", text.replace(" ", ""))
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    return v if 5 <= v <= 5000 else None


def _parse_first_int(text: str) -> int | None:
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


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
    print(f"\nTotal Agence Simon (Amboise): {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:12]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('pieces') or '?'}p/{b.get('chambres') or '?'}ch"
            f" — {b['ville']} — {len(b['photos'])} photos"
        )

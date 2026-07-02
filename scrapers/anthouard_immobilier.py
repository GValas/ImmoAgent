"""scrapers/anthouard_immobilier.py — Anthouard Immobilier (agence Dordogne / Sud-Ouest)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress + Elementor, serveur LiteSpeed).

URL pattern : /proprietes-a-vendre-en-dordogne/  (page unique, SANS pagination :
              les ~200 cartes sont toutes injectées dans le HTML SSR — boucle
              Elementor `e-loop-item`). Le site est MONO-ZONE : toutes les pages
              thématiques sont « ...-a-vendre-en-dordogne » ; il n'existe aucun
              slug par département. Les biens sont en Dordogne (24) et quelques
              communes limitrophes du Sud-Ouest (47, 33, 46, 19, 87, 24).

Stratégie filtre département :
  Le site n'expose AUCUN code postal (ni sur la carte, ni sur le détail) ; la
  localisation n'apparaît que sous forme de nom de ville/secteur dans le titre.
  → On résout le département via un gazetteer ville→dept (zone Périgord/Sud-Ouest)
    puis on POST-FILTRE STRICTEMENT sur `code_postal[:2] in departements`.
    Toute carte dont le département ne peut pas être déterminé OU n'est pas dans
    la zone cible est ÉCARTÉE (conservateur → 0 fuite garantie).

Cartes : .e-loop-item  (classe Elementor ; chaque carte porte aussi `.bien`)
  - URL    : a[href]  → /propriete-a-vendre/{slug}/
  - Titre  : premier <h*> de la carte (préfixé par STATUT : VENDUE / SOUS OFFRE…)
  - Prix   : "150 000 €" dans le texte de la carte
  - Surface: "75 m²" (1ʳᵉ occurrence m²) ; terrain : "1,4 ha" ou "4175 m²" ensuite
  - Réf    : "Réf. 429"
  - Photos : img[data-src] (lazy-load ; src réel dans data-src)

Particularités :
  - Page unique très volumineuse (~3.7 Mo) ; un seul GET par recherche.
  - Beaucoup de biens « VENDUE / VENDU / SOUS COMPROMIS » → écartés.
  - Segment de prestige rural (chartreuses, manoirs, moulins, demeures de charme).

NB : la zone cible actuelle du projet (72/28/45/89, Val-de-Loire/Ouest) est hors
     du rayon de cette agence → ce scraper renvoie 0 bien tant que la zone reste
     au nord. Conservé fonctionnel pour une éventuelle extension Sud-Ouest.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.anthouardimmobilier.com"
LISTING_URL = f"{BASE_URL}/proprietes-a-vendre-en-dordogne/"
PHOTOS_PER_CARD = 10


# Statuts de carte qui signalent un bien indisponible → on écarte
_SOLD = re.compile(r"VENDU|SOUS\s+OFFRE|SOUS\s+COMPROMIS|COMPROMIS", re.IGNORECASE)

# Gazetteer ville/secteur → département (zone Périgord & Sud-Ouest couverte par
# l'agence). Le site n'expose pas de code postal ; on déduit le dept du nom de
# commune ou de secteur présent dans le titre. Clés en minuscules sans accents.
# Toute carte non résolue est ÉCARTÉE (conservateur, anti-fuite).
VILLE_DEPT: dict[str, str] = {
    # Dordogne (24) — secteurs et communes les plus fréquents
    "dordogne": "24", "perigord": "24", "perigord noir": "24",
    "perigord vert": "24", "perigord blanc": "24", "perigord pourpre": "24",
    "bergerac": "24", "perigueux": "24", "sarlat": "24", "montignac": "24",
    "monpazier": "24", "tremolat": "24", "le bugue": "24", "lalinde": "24",
    "issigeac": "24", "eymet": "24", "belves": "24", "le buisson": "24",
    "saint-cyprien": "24", "domme": "24", "terrasson": "24", "thiviers": "24",
    "nontron": "24", "brantome": "24", "riberac": "24", "mussidan": "24",
    "vergt": "24", "villamblard": "24", "saint-astier": "24", "neuvic": "24",
    "excideuil": "24", "hautefort": "24", "montpon": "24", "vélines": "24",
    "velines": "24", "sigoules": "24", "beaumont": "24", "villefranche": "24",
    "rouffignac": "24", "le coux": "24", "siorac": "24", "cadouin": "24",
    # Lot-et-Garonne (47) — limitrophe sud
    "lot-et-garonne": "47", "villeneuve-sur-lot": "47", "agen": "47",
    "marmande": "47", "duras": "47", "miramont": "47", "casseneuil": "47",
    "fumel": "47", "monflanquin": "47", "tournon-d'agenais": "47",
    # Gironde (33) — limitrophe ouest
    "gironde": "33", "sainte-foy-la-grande": "33", "castillon": "33",
    "libourne": "33",
    # Lot (46) — limitrophe est
    "lot": "46", "gourdon": "46", "souillac": "46", "cahors": "46",
    "saint-cere": "46",
    # Corrèze (19) — limitrophe nord-est
    "correze": "19", "brive": "19", "objat": "19", "terrasson-correze": "19",
    # Haute-Vienne (87) — limitrophe nord
    "haute-vienne": "87", "limoges": "87", "saint-yrieix": "87",
    # Charente (16) — limitrophe nord-ouest
    "charente": "16", "angouleme": "16", "aubeterre": "16",
}

# Codes postaux (préfixe dept) connus → nom de département pour le champ texte.
DEPT_NOMS: dict[str, str] = {
    "24": "Dordogne", "47": "Lot-et-Garonne", "33": "Gironde",
    "46": "Lot", "19": "Corrèze", "87": "Haute-Vienne", "16": "Charente",
}


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
    ) as client:
        try:
            r = await client.get(LISTING_URL)
        except Exception as e:
            print(f"[Anthouard] Erreur réseau: {e}")
            return results

        if r.status_code != 200:
            print(f"[Anthouard] Statut HTTP {r.status_code}")
            return results

        cards = BeautifulSoup(r.text, "html.parser").select(".e-loop-item")
        if not cards:
            print("[Anthouard] Aucune carte .e-loop-item trouvée")
            return results

        seen_ids: set[str] = set()
        kept_by_dept: dict[str, int] = {}

        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue

            dept = bien["departement"]
            # Post-filtre STRICT : on n'accepte que les départements demandés.
            if dept not in departements:
                continue
            # Sécurité supplémentaire via code_postal si présent.
            if bien["code_postal"] and bien["code_postal"][:2] != dept:
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
            kept_by_dept[dept] = kept_by_dept.get(dept, 0) + 1

        if kept_by_dept:
            detail = ", ".join(f"{d}:{n}" for d, n in sorted(kept_by_dept.items()))
            print(f"[Anthouard] {len(results)} annonces retenues ({detail})")
        else:
            print(
                f"[Anthouard] 0 annonce dans la zone demandée "
                f"({len(cards)} cartes scannées ; agence Dordogne/Sud-Ouest)"
            )

    return results


def _parse_card(card) -> dict | None:
    link = card.find("a", href=True)
    href = link.get("href", "") if link else ""
    if not href or "/propriete-a-vendre/" not in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Titre : premier heading de la carte (préfixé du statut)
    head = card.find(["h1", "h2", "h3", "h4"])
    titre_raw = head.get_text(" ", strip=True) if head else ""

    text = card.get_text(" ", strip=True)

    # On écarte les biens vendus / sous offre / sous compromis
    if _SOLD.search(titre_raw) or _SOLD.search(text):
        return None

    # Nettoyage du titre (retire un éventuel préfixe de statut résiduel)
    titre = _SOLD.sub("", titre_raw).strip(" :–-").strip() or titre_raw

    # Référence (id_annonce)
    ref = None
    m_ref = re.search(r"R[ée]f\.?\s*(\d+)", text, re.IGNORECASE)
    if m_ref:
        ref = m_ref.group(1)
    # secours : slug d'URL
    slug = [p for p in href.split("/") if p][-1] if href else ""
    id_annonce = f"anthouard-{ref}" if ref else (slug or url)

    # Prix : "150 000 €"
    prix = _parse_price(text)

    # Surface habitable : 1ʳᵉ occurrence "NN m²"
    surface = _parse_surface(text)

    # Terrain : "1,4 ha" ou "4175 m²" (occurrence postérieure à la surface hab)
    surface_terrain = _parse_terrain(text)

    # Localisation → département via gazetteer (titre + secteur)
    dept, ville = _resolve_dept(titre, text)
    if dept is None:
        # Département indéterminé → on écarte (anti-fuite) tout en signalant
        # implicitement via departement = "" qui sera rejeté par le post-filtre.
        dept = ""

    departement = dept

    return {
        "source": "anthouard_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": _type_bien(titre, url),
        "description": titre[:1200],
        "departement": departement,
        "ville": (ville or "")[:80],
        "code_postal": "",  # le site n'expose pas de CP
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": _photos(card),
        "dpe": None,
        "agence": "Anthouard Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    m = re.search(r"([\d][\d\s\xa0\.]{2,})\s*€", text)
    if not m:
        return None
    cleaned = re.sub(r"[\s\xa0\.]", "", m.group(1))
    try:
        v = float(cleaned)
        return v if v > 1000 else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """1ʳᵉ occurrence 'NN m²' = surface habitable."""
    m = re.search(r"(\d[\d\s\xa0]*)\s*m²", text)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 3000:
                return f
        except ValueError:
            pass
    return None


def _parse_terrain(text: str) -> float | None:
    """Terrain : 'X,Y ha' (→ m²) ou 'NNNN m²' (2ᵉ occurrence, > surface hab)."""
    # hectares en priorité (format Anthouard : "1,4 ha", "47 ha")
    m_ha = re.search(r"([\d]+(?:[,\.]\d+)?)\s*ha", text, re.IGNORECASE)
    if m_ha:
        try:
            return round(float(m_ha.group(1).replace(",", ".")) * 10000)
        except ValueError:
            pass
    # sinon, les occurrences "m²" : la 2ᵉ est généralement le terrain
    occ = re.findall(r"(\d[\d\s\xa0]*)\s*m²", text)
    if len(occ) >= 2:
        val = re.sub(r"[\s\xa0]", "", occ[1])
        try:
            f = float(val)
            if f >= 50:
                return f
        except ValueError:
            pass
    return None


def _resolve_dept(titre: str, text: str) -> tuple[str | None, str | None]:
    """Déduit (dept, ville) du gazetteer à partir du titre/secteur.

    Retourne (None, None) si aucune correspondance → la carte sera écartée.
    """
    hay = _strip_accents(f"{titre} {text}").lower()
    best_dept = None
    best_ville = None
    best_len = 0
    for key, dept in VILLE_DEPT.items():
        # match sur mot/segment ; on privilégie la clé la plus longue trouvée
        if re.search(r"\b" + re.escape(key) + r"\b", hay):
            if len(key) > best_len:
                best_len = len(key)
                best_dept = dept
                best_ville = key.title()
    return best_dept, best_ville


def _type_bien(titre: str, url: str) -> str:
    hay = f"{titre} {url}".lower()
    for kw, label in [
        ("chateau", "château"), ("château", "château"),
        ("manoir", "manoir"), ("chartreuse", "chartreuse"),
        ("moulin", "moulin"), ("ferme", "ferme"), ("perigourdine", "propriété"),
        ("domaine", "domaine"), ("gite", "propriété"), ("gîte", "propriété"),
        ("maison de maitre", "maison de maître"), ("maison de maître", "maison de maître"),
        ("demeure", "demeure"), ("propriete", "propriété"), ("propriété", "propriété"),
        ("appartement", "appartement"), ("villa", "villa"),
        ("maison", "maison"),
    ]:
        if kw in hay:
            return label
    return "propriété"


def _photos(card) -> list[str]:
    photos: list[str] = []
    for img in card.find_all("img"):
        src = (
            img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("data-lazy")
            or img.get("src")
            or ""
        )
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            if src not in photos:
                photos.append(src)
    return photos[:PHOTOS_PER_CARD]


def _strip_accents(s: str) -> str:
    table = str.maketrans(
        "àâäáãéèêëíìîïóòôöõúùûüçñ", "aaaaaeeeeiiiiooooouuuucn"
    )
    return s.translate(table)


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
    print(f"\nTotal Anthouard: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

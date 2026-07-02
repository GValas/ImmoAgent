"""scrapers/metais_immobilier.py — Cabinet Métais (châteaux, manoirs, demeures de caractère)

Méthode : scrape_simple (httpx pur, SSR WordPress sous nginx). Pas de Playwright.

Site spécialisé Touraine / Val de Loire (châteaux, manoirs, maisons de caractère,
moulins, domaines, vignobles…). Petit inventaire (≈5 biens), tous en Indre-et-Loire (37).

Listing : https://metais-immobilier.com/biens-immobiliers/
  - cards dans div#all_biens > .column > .card
  - prix   : .card-price          ("904 000 €")
  - titre  : h2.title a           ("TOURAINE-ANJOU, entre CHINON et SAUMUR")
  - réf.   : h2 .is-size-7         ("Référence : 1332")
  - photo  : .card-image img[src]
  - URL détail : h2 a[href]  (slug, pas de filtre serveur)

Filtre département : AUCUN code postal ni ville structurée sur le site (la seule
adresse 37500 CHINON présente est celle de l'agence, en pied de page). La localisation
du bien n'apparaît QUE dans le titre sous forme de noms de régions/communes
("TOURAINE", "CHINON", "MONTBAZON"…). On en déduit le département via une table de
mots-clés (toponymes → dept), avec repli "TOURAINE" → 37. Post-filtre sur ce dept.
Biens sans dept identifiable : écartés si une liste de départements est fournie (0 fuite).

Détail (par bien, ~5 requêtes) : div.column.is-half  <strong>Label</strong><br>valeur
  - Type de bien / Surface / Nombre de pièces / Nombre de chambres / Surface du terrain

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://metais-immobilier.com"
LISTING_URL = f"{BASE_URL}/biens-immobiliers/"


# Exclusion par TYPE de bien (résolu sur la fiche détail, pas sur le titre :
# un titre peut citer une "forêt domaniale" voisine sans être une forêt à vendre).
_EXCLUDE_TYPES = re.compile(r"appartement|studio|immeuble|terrain\b|for[êe]t|location", re.I)
_TYPE_MAP = [
    (re.compile(r"château|chateau", re.I), "château"),
    (re.compile(r"manoir", re.I), "manoir"),
    (re.compile(r"moulin", re.I), "moulin"),
    (re.compile(r"longère|longere", re.I), "longère"),
    (re.compile(r"domaine", re.I), "domaine"),
    (re.compile(r"propriété|propriete|demeure", re.I), "propriété"),
    (re.compile(r"maison", re.I), "maison"),
]

# Toponymes (régions/communes du Val de Loire) → département.
# La localisation n'apparaît que dans le titre ; on la résout par mots-clés.
# Ordre : du plus spécifique (commune) au plus large (région).
_DEPT_KEYWORDS = [
    # Indre-et-Loire (37) — Touraine
    (re.compile(r"\b(touraine|tours|chinon|montbazon|azay[\s\-]le[\s\-]rideau|langeais|"
                r"rigny[\s\-]uss[ée]|amboise|loches|bourgueil|richelieu|sainte?[\s\-]maure)\b", re.I), "37"),
    # Maine-et-Loire (49) — Anjou / Saumur
    (re.compile(r"\b(anjou|saumur|angers|fontevraud|cholet|baug[ée])\b", re.I), "49"),
    # Loir-et-Cher (41) — Sologne / Blois
    (re.compile(r"\b(loir[\s\-]et[\s\-]cher|blois|chambord|cheverny|sologne|vend[ôo]me|romorantin)\b", re.I), "41"),
    # Sarthe (72)
    (re.compile(r"\b(sarthe|le mans|la fl[èe]che|sabl[ée])\b", re.I), "72"),
    # Indre (36)
    (re.compile(r"\b(\bindre\b|ch[âa]teauroux|argenton|le blanc|valen[çc]ay)\b", re.I), "36"),
    # Cher (18) — Berry
    (re.compile(r"\b(\bcher\b|bourges|sancerre|aubigny[\s\-]sur[\s\-]n[èe]re|vierzon)\b", re.I), "18"),
    # Loiret (45)
    (re.compile(r"\b(loiret|orl[ée]ans|gien|montargis|pithiviers|sully[\s\-]sur[\s\-]loire)\b", re.I), "45"),
    # Eure-et-Loir (28)
    (re.compile(r"\b(eure[\s\-]et[\s\-]loir|chartres|ch[âa]teaudun|n[oô]gent[\s\-]le[\s\-]rotrou|dreux)\b", re.I), "28"),
    # Yonne (89)
    (re.compile(r"\b(yonne|auxerre|sens|avallon|tonnerre|chablis)\b", re.I), "89"),
    # Nièvre (58)
    (re.compile(r"\b(ni[èe]vre|nevers|cosne[\s\-]sur[\s\-]loire|clamecy|d[ée]cize)\b", re.I), "58"),
    # Mayenne (53)
    (re.compile(r"\b(mayenne|laval|ch[âa]teau[\s\-]gontier|ern[ée])\b", re.I), "53"),
]


def _dept_from_title(title: str) -> str:
    for rx, dept in _DEPT_KEYWORDS:
        if rx.search(title):
            return dept
    return ""


def _ville_from_title(title: str) -> str:
    """Heuristique : on prend la première commune en MAJUSCULES du titre."""
    # Communes citées en capitales (CHINON, MONTBAZON, TOURS…)
    _SKIP = {"TOURAINE", "ANJOU", "REGION", "RÉGION", "VILLE", "TOURAINE-ANJOU", "BERRY", "SOLOGNE"}
    for tok in re.findall(r"\b([A-ZÉÈÀÂÎÔÛ][A-ZÉÈÀÂÎÔÛ\-’']{2,})\b", title):
        if tok not in _SKIP:
            return tok.title()
    return ""


def _parse_num(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", (text or "").replace("\xa0", " "))
    return float(cleaned) if cleaned else None


def _parse_terrain_m2(text: str) -> float | None:
    """'12 hectares 63 ares 96 centiares' / '5000 m²' → m²"""
    if not text:
        return None
    t = text.lower().replace("\xa0", " ")
    m_ha = re.search(r"(\d[\d\s]*)\s*hectare", t)
    m_a = re.search(r"(\d[\d\s]*)\s*ares?\b", t)
    m_ca = re.search(r"(\d[\d\s]*)\s*centiare", t)
    m_m2 = re.search(r"(\d[\d\s]*)\s*m²", t)
    if m_ha or m_a or m_ca:
        ha = int(re.sub(r"\s", "", m_ha.group(1))) if m_ha else 0
        a = int(re.sub(r"\s", "", m_a.group(1))) if m_a else 0
        ca = int(re.sub(r"\s", "", m_ca.group(1))) if m_ca else 0
        return float(ha * 10000 + a * 100 + ca)
    if m_m2:
        return float(re.sub(r"\s", "", m_m2.group(1)))
    # valeur nue sans unité (ex. "1075") → m²
    m_bare = re.search(r"\d[\d\s]*", t)
    if m_bare:
        return float(re.sub(r"\s", "", m_bare.group(0)))
    return None


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25) as client:
        try:
            r = await client.get(LISTING_URL)
            r.raise_for_status()
        except Exception as e:
            print(f"[MetaisImmobilier] Erreur listing : {e}")
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        cont = soup.find(id="all_biens")
        cards = cont.select(".card") if cont else []

        biens, seen = [], set()
        for card in cards:
            bien = _parse_card(card)
            if not bien:
                continue
            if bien["id_annonce"] in seen:
                continue
            seen.add(bien["id_annonce"])
            biens.append(bien)

        # Enrichissement détail (surface, pièces, chambres, terrain, type) — petit inventaire.
        await asyncio.gather(*(_enrich(client, b) for b in biens))

    results = []
    for b in biens:
        # Exclusion par type réel (fiche détail), repli sur le type déduit du titre.
        type_raw = b.pop("_type_raw", "") or b.get("type_bien", "")
        if _EXCLUDE_TYPES.search(type_raw):
            continue

        # POST-FILTRE département (déduit du titre). 0 fuite : si une liste de depts
        # est fournie, on écarte tout bien dont le dept n'est pas identifié OU hors zone.
        if departements:
            if not b["departement"] or b["departement"] not in departements:
                continue

        p = b.get("prix") or 0
        s = b.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        results.append(b)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"] or "??"] = by_dept.get(b["departement"] or "??", 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[MetaisImmobilier] Dept {dept}: {n} annonce(s)")

    return results


def _parse_card(card) -> dict | None:
    try:
        a = card.select_one("h2.title a") or card.select_one("h2 a")
        if not a or not a.get("href"):
            return None
        url = a["href"].strip()
        titre = a.get_text(" ", strip=True)
        titre = re.sub(r"\s+", " ", titre).strip().rstrip(",").strip()

        ref_el = card.select_one("h2 .is-size-7")
        ref = None
        if ref_el:
            m = re.search(r"(\d+)", ref_el.get_text())
            ref = m.group(1) if m else None
        id_annonce = ref or url.rstrip("/").split("/")[-1]

        prix = None
        pr = card.select_one(".card-price")
        if pr:
            prix = _parse_num(pr.get_text())

        photos = []
        img = card.select_one(".card-image img")
        if img:
            src = img.get("src") or ""
            if src.startswith("http"):
                photos.append(src)

        dept = _dept_from_title(titre)
        ville = _ville_from_title(titre)

        type_bien = "propriété"
        for rx, label in _TYPE_MAP:
            if rx.search(titre):
                type_bien = label
                break

        return {
            "source": "metais_immobilier",
            "url": url,
            "id_annonce": id_annonce,
            "titre": titre[:200],
            "type_bien": type_bien,
            "description": None,
            "departement": dept,
            "ville": ville,
            "code_postal": "",
            "surface": None,
            "surface_terrain": None,
            "pieces": None,
            "chambres": None,
            "prix": prix,
            "dpe": None,
            "photos": photos,
            "agence": "Cabinet Métais",
        }
    except Exception:
        return None


async def _enrich(client: httpx.AsyncClient, bien: dict) -> None:
    """Récupère surface/pièces/chambres/terrain/type sur la fiche détail."""
    try:
        r = await client.get(bien["url"])
        if r.status_code != 200:
            return
        soup = BeautifulSoup(r.text, "html.parser")

        # Blocs spec : div.column.is-half = <strong>Label</strong><br>valeur.
        # On lit UNIQUEMENT le(s) nœud(s) texte qui suivent le <strong>, pas tout
        # le contenu du div (certains div.column contiennent toute la description).
        specs: dict[str, str] = {}
        for col in soup.select("div.column.is-half"):
            strong = col.find("strong", recursive=False) or col.find("strong")
            if not strong:
                continue
            label = strong.get_text(strip=True)
            parts = []
            for sib in strong.next_siblings:
                txt = sib.get_text(" ", strip=True) if hasattr(sib, "get_text") else str(sib).strip()
                if txt:
                    parts.append(txt)
            val = " ".join(parts).strip()
            # garde-fou : une valeur de spec est courte (pas un paragraphe entier)
            if label and val and len(val) < 80:
                specs.setdefault(label, val)

        if "Type de bien" in specs:
            t = specs["Type de bien"].lower()
            bien["_type_raw"] = specs["Type de bien"]
            for rx, label in _TYPE_MAP:
                if rx.search(t):
                    bien["type_bien"] = label
                    break
        if "Surface" in specs:
            bien["surface"] = _parse_num(specs["Surface"])
        if "Nombre de pièces" in specs:
            m = re.search(r"\d+", specs["Nombre de pièces"])
            bien["pieces"] = int(m.group()) if m else None
        if "Nombre de chambres" in specs:
            m = re.search(r"\d+", specs["Nombre de chambres"])
            bien["chambres"] = int(m.group()) if m else None
        if "Surface du terrain" in specs:
            bien["surface_terrain"] = _parse_terrain_m2(specs["Surface du terrain"])
    except Exception:
        return


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    depts = criteres.departements

    print(f"Départements ciblés : {depts}\n")
    biens = asyncio.run(
        search({
            "departements": depts,
            "prix_max": getattr(criteres, "prix_max", 0),
            "prix_min": getattr(criteres, "prix_min", 0),
            "surface_min": getattr(criteres, "surface_min", 0),
        })
    )
    print(f"\nTotal Cabinet Métais (depts cibles) : {len(biens)}")
    leaks = [b for b in biens if b["departement"] and b["departement"] not in {str(d).zfill(2) for d in depts}]
    print(f"FUITES hors-département : {len(leaks)}")
    for b in biens:
        print(
            f"  [{b['departement']}] {b['type_bien']:<10} {b['ville']:<12} "
            f"{b['prix']}€ — {b.get('surface')}m² — {b.get('pieces')}p/{b.get('chambres')}ch "
            f"— terrain {b.get('surface_terrain')}m² — {b['titre'][:40]}"
        )

    # Contrôle anti-fuite : run sans restriction de département (inventaire complet)
    print("\n--- Inventaire complet (sans filtre dept) ---")
    allb = asyncio.run(search({"departements": []}))
    print(f"Total inventaire : {len(allb)}")
    from collections import Counter
    print("Répartition dept :", dict(Counter(b["departement"] or "??" for b in allb)))

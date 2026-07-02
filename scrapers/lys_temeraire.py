"""scrapers/lys_temeraire.py — Le Lys Téméraire (agence immobilier de charme / prestige Bourgogne)

Méthode : scrape_simple (httpx) — SSR HTML statique (templates Dreamweaver, pas de JS).

Particularité du site : pas de page liste paginée classique. Chaque CATÉGORIE est un
SOUS-DOMAINE (demeure./manoir./moulin./etang./equestre./gite..lys-temeraire.com) dont
l'`index.htm` référence les fiches. Les fiches elles-mêmes vivent sur ces sous-domaines
(et sur des domaines sœurs .biz/.fr du même opérateur, ignorés ici pour ne pas doublonner
demeure-prestige.biz / immobilier-bourgogne.biz).

URL pattern fiche : https://{cat}.lys-temeraire.com/{NN}-{ville}-{type}-a-vendre-...htm
  → le CODE DÉPARTEMENT (NN) est en TÊTE du slug ⇒ post-filtre dept fiable et strict.

Stratégie :
  1. Crawl des index.htm des sous-domaines catégories → collecte des liens fiches
     (href .htm dont le chemin commence par "NN-"), uniquement sur *.lys-temeraire.com.
  2. Dédup, puis fetch de chaque fiche.
  3. Parse depuis la fiche : <title> (type, pièces, surface, ville, dept) + "Prix : N €".
  4. Post-filtre STRICT sur le code dept de tête de slug ∈ départements cibles.

Couverture réelle : Bourgogne (Nièvre 58, Yonne 89 essentiellement ; aussi 21, 03 hors zone).
Inventaire de niche → faible volume mais réel sur 58/89.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS
from scrapers._base import parse_int as _parse_int

BASE_DOMAIN = "lys-temeraire.com"
# Sous-domaines catégories servant de pages d'inventaire
CATEGORIES = ["demeure", "manoir", "moulin", "etang", "equestre", "gite"]
PHOTOS_PER_CARD = 12


# Codes postaux des préfectures par département (le site donne rarement un CP exact ;
# on reconstruit un CP plausible à partir du code dept pour rester homogène avec le modèle).
# Le filtrage repose sur le code dept (NN) de tête de slug, pas sur ce CP.
_DEPT_NAMES = {
    "58": "Nièvre", "89": "Yonne", "72": "Sarthe", "28": "Eure-et-Loir",
    "45": "Loiret", "49": "Maine-et-Loire", "37": "Indre-et-Loire", "36": "Indre",
    "18": "Cher", "41": "Loir-et-Cher", "53": "Mayenne",
}

_TYPE_KEEP = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|mas|g[iî]te|corps-de-ferme|haras|fermette|maison-forte",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_urls: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1. Collecte des liens fiches depuis les index des catégories
        listing_urls: set[str] = set()
        for cat in CATEGORIES:
            idx = f"https://{cat}.{BASE_DOMAIN}/index.htm"
            try:
                links = await _collect_listings(client, idx)
                listing_urls.update(links)
            except Exception as e:
                print(f"[LysTemeraire] Erreur index {cat}: {e}")
            await asyncio.sleep(0.5)

        print(f"[LysTemeraire] {len(listing_urls)} fiches candidates collectées")

        # 2. Fetch + parse de chaque fiche
        for url in sorted(listing_urls):
            dept = _dept_from_url(url)
            if dept not in departements:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            try:
                bien = await _scrape_detail(client, url, dept)
            except Exception as e:
                print(f"[LysTemeraire] Erreur fiche {url}: {e}")
                bien = None
            if not bien:
                continue

            # Post-filtre dept STRICT (sécurité : le code dept doit rester dans la zone)
            if bien["departement"] not in departements:
                continue

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            results.append(bien)
            await asyncio.sleep(0.5)

    print(f"[LysTemeraire] {len(results)} biens retenus (zone cible)")
    return results


async def _collect_listings(client: httpx.AsyncClient, index_url: str) -> set[str]:
    """Récupère les URLs de fiches (chemin commençant par 'NN-') sur *.lys-temeraire.com."""
    r = await client.get(index_url)
    if r.status_code != 200:
        return set()
    base = re.match(r"(https?://[^/]+)", str(r.url)).group(1)
    out: set[str] = set()
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if not href or not href.endswith(".htm") and ".htm?" not in href:
            continue
        if href.startswith("http"):
            full = href
        else:
            full = base + "/" + href.lstrip("/")
        # Restreint au domaine lys-temeraire.com (on ignore les domaines sœurs .biz/.fr)
        if f".{BASE_DOMAIN}/" not in full and not full.endswith(f".{BASE_DOMAIN}"):
            continue
        path = full.split("//", 1)[-1].split("/", 1)[-1] if "/" in full.split("//", 1)[-1] else ""
        if "index.htm" in path or not path:
            continue
        # Chemin (dernier segment) commençant par un code dept "NN-"
        last = path.rstrip("/").split("/")[-1]
        if re.match(r"\d{2}-", last):
            out.add(full)
    return out


def _dept_from_url(url: str) -> str:
    last = url.rstrip("/").split("/")[-1]
    m = re.match(r"(\d{2})-", last)
    return m.group(1) if m else ""


async def _scrape_detail(client: httpx.AsyncClient, url: str, dept: str) -> dict | None:
    r = await client.get(url)
    if r.status_code != 200:
        return None
    t = r.text
    soup = BeautifulSoup(t, "html.parser")

    title_el = soup.find("title")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        return None
    # Soft-404 : le site renvoie 200 avec un titre/corps "Erreur 404" pour les fiches retirées
    if re.search(r"erreur\s*404|page\s+introuvable|404\b", titre, re.IGNORECASE):
        return None

    type_bien = _type_from(titre, url)
    if not type_bien:
        return None

    prix = _parse_price(t)
    surface = _parse_surface_hab(titre)
    pieces = _parse_int(r"(\d+)\s*pi[eè]ces?", titre)
    surface_terrain = _parse_terrain(titre)

    ville = _ville_from(url, titre, dept)
    code_postal = _cp_from(titre, dept)

    # Description : meta description sinon corps de texte nettoyé
    description = ""
    md = soup.find("meta", attrs={"name": "description"})
    if md and md.get("content"):
        description = md["content"].strip()
    if len(description) < 40:
        body = soup.get_text(" ", strip=True)
        m = re.search(r"Prix\s*:", body)
        if m:
            description = body[max(0, m.start() - 700):m.start()].strip()

    photos: list[str] = []
    base = re.match(r"(https?://[^/]+)", str(r.url)).group(1)
    for img in soup.select("img[src]"):
        src = img.get("src", "").strip()
        if not src or src.startswith("data:"):
            continue
        if src.lower().endswith((".png",)) and "/images/" in src:
            continue  # pictos / logos
        if not re.search(r"\.(jpe?g|png|webp)", src, re.IGNORECASE):
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif not src.startswith("http"):
            src = base + "/" + src.lstrip("/")
        if src not in photos:
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "lys_temeraire",
        "url": url,
        "id_annonce": url.rstrip("/").split("/")[-1].replace(".htm", "")[:120],
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
        "photos": photos,
        "dpe": None,
        "agence": "Le Lys Téméraire",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _type_from(titre: str, url: str) -> str | None:
    src = titre + " " + url
    m = _TYPE_KEEP.search(src)
    if not m:
        return None
    word = m.group(0).lower()
    mapping = {
        "propriete": "propriété", "propriété": "propriété", "longere": "longère",
        "longère": "longère", "chateau": "château", "château": "château",
        "gite": "gîte", "gîte": "gîte", "maison-forte": "maison forte",
        "corps-de-ferme": "corps de ferme",
    }
    return mapping.get(word, word)


def _parse_price(text: str) -> float | None:
    m = re.search(r"Prix\s*:\s*([\d\s\xa0\.]+)\s*€", text)
    if not m:
        return None
    cleaned = re.sub(r"[\s\xa0\.]", "", m.group(1))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_surface_hab(text: str) -> float | None:
    """Surface habitable depuis le titre : 'NNN m²' (hectares ignorés)."""
    if not text:
        return None
    # On évite les surfaces en ha (terrain) : on cherche un nombre suivi de m²
    for m in re.finditer(r"(\d[\d\s\xa0]*)\s*m[²2]\b", text):
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
        except ValueError:
            continue
        if 8 <= f <= 3000:
            return f
    return None


def _parse_terrain(text: str) -> float | None:
    """Terrain depuis le titre : 'N,N ha' ou 'NN ha' → m². Sinon None."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*ha\b", text, re.IGNORECASE)
    if m:
        try:
            return round(float(m.group(1).replace(",", ".")) * 10000)
        except ValueError:
            pass
    return None


def _ville_from(url: str, titre: str, dept: str) -> str:
    """Ville : depuis le titre ('proche XXX,' / 'à XXX,') sinon depuis le slug."""
    m = re.search(r"(?:proche|à|en|dans)\s+([A-ZÉÈÀ][\wÀ-ÿ'\- ]+?),", titre)
    if m:
        v = m.group(1).strip()
        if len(v) > 1:
            return v
    # Repli : 2e segment du slug (après le code dept)
    last = url.rstrip("/").split("/")[-1].replace(".htm", "")
    parts = last.split("-")
    if len(parts) > 1:
        # rassemble jusqu'au mot-clé type/a
        buf = []
        for p in parts[1:]:
            if p in ("a", "vendre", "maison", "moulin", "propriete", "chateau",
                     "manoir", "demeure", "domaine", "ferme", "etang", "yonne",
                     "nievre", "puisaye"):
                break
            buf.append(p)
        if buf:
            return " ".join(w.capitalize() for w in buf)
    return _DEPT_NAMES.get(dept, "")


def _cp_from(titre: str, dept: str) -> str:
    """Cherche un CP à 5 chiffres dans le titre, sinon chaîne vide (le filtre est sur le dept)."""
    m = re.search(rf"\b({dept}\d{{3}})\b", titre)
    return m.group(1) if m else ""


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
    print(f"\nTotal Lys Téméraire: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens if b["departement"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:60]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

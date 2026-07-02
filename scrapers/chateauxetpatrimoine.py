"""scrapers/chateauxetpatrimoine.py — Châteaux et Patrimoine (agence FNAIM patrimoine)

Méthode : scrape_simple (httpx) — SSR WordPress (thème Suki), pas de JS, pas de
          Cloudflare. Agence spécialisée châteaux / manoirs / demeures de
          caractère / domaines, couverture nationale (forte au Sud-Ouest).

URL pattern :
  - Listing UNIQUE toutes-régions : /proprietes/  (toutes les annonces sur une
    seule page, ~54 biens, PAS de pagination observée).
  - Détail : /proprietes/{slug}/

Filtre département — FIABLE, par classe CSS de l'article :
  chaque <article class="... localisation-{region-slug} localisation-{dept}-{NN}">
  porte un code département à 2 chiffres (ex. `localisation-indre-36`,
  `localisation-nievre-58`). On lit ce code → 0 fuite par construction
  (aucune dépendance à un CP parfois absent). On ne retient que les départements
  cibles, puis on re-vérifie le préfixe CP[:2] quand un CP est trouvé.

Cartes (listing) : article.entry
  - dept   : classe `localisation-...-{NN}`
  - titre  : h2.entry-title a  (+ href = URL détail)
  - texte  : div.entry-content p (description courte)

Détail (/proprietes/{slug}/) — pour prix/surface/DPE/photos (la carte ne les
porte pas) :
  - prix    : "Prix : 1 160 000 €"
  - surface : "Surface : 330 m²"
  - réf     : "Ref ... : 2153"
  - DPE     : "DPE : 271 F   GES : 85 F"  → on garde la lettre
  - terrain / pièces / chambres : dans le texte libre (parseur best-effort)
  - photos  : https://.../wp-content/uploads/AAAA/MM/...jpg (hors logos FNAIM)

Particularités :
  - Site de PRESTIGE : prix souvent > prix_max cible (300–600 k€). Le scraper est
    fonctionnel ; le filtre prix écarte la majorité — c'est attendu.
  - Pas de CP/ville structurés : la ville est devinée depuis titre/description
    (souvent une province « Berry », « Nivernais »…). Le département reste sûr
    via la classe CSS. `code_postal` est donc souvent None ; le post-filtre
    s'appuie alors sur le code dept de la classe (pas de fuite).
  - Quelques fetches détail par run uniquement (sur les seuls biens in-zone).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.chateauxetpatrimoine.com"
LISTING_URL = f"{BASE_URL}/proprietes/"
PHOTOS_PER_BIEN = 12


# Types de bien que l'on conserve (titre / description)
_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|mas|gentilhommi[eè]re|gentilhommiere|bastide|chartreuse|"
    r"maison de ma[iî]tre|corps de ferme|haras",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        try:
            r = await client.get(LISTING_URL)
        except Exception as e:
            print(f"[ChateauxPatrimoine] Erreur listing : {e}")
            return results

        if r.status_code != 200:
            print(f"[ChateauxPatrimoine] Listing status {r.status_code}")
            return results

        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("article.entry")

        # 1) Sélection des cartes in-zone (dept lu dans la classe CSS)
        in_zone: list[tuple[str, str, str, str]] = []  # (dept, url, titre, desc)
        for card in cards:
            cls = " ".join(card.get("class", []))
            m = re.search(r"localisation-[a-z0-9-]*?-(\d{2})\b", cls)
            if not m:
                continue
            dept = m.group(1)
            if dept not in departements:
                continue

            link = card.select_one("h2.entry-title a")
            if not link or not link.get("href"):
                continue
            url = link["href"]
            titre = link.get_text(" ", strip=True)

            desc_el = card.select_one(".entry-content")
            desc = desc_el.get_text(" ", strip=True) if desc_el else ""

            # Filtre type sur titre + description
            if not _KEEP_TYPE.search(f"{titre} {desc}"):
                continue

            in_zone.append((dept, url, titre, desc))

        print(
            f"[ChateauxPatrimoine] {len(cards)} annonces, "
            f"{len(in_zone)} dans la zone cible"
        )

        # 2) Fetch détail (prix/surface/DPE/photos) sur les seuls biens in-zone
        for dept, url, titre, desc in in_zone:
            try:
                bien = await _scrape_detail(client, dept, url, titre, desc)
            except Exception as e:
                print(f"[ChateauxPatrimoine] Erreur détail {url}: {e}")
                continue
            if not bien:
                continue

            # Post-filtre dept STRICT : code dept connu de façon fiable (classe CSS).
            # Si un CP a été extrait, il doit concorder.
            if bien["code_postal"] and bien["code_postal"][:2] != dept:
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

    print(f"[ChateauxPatrimoine] {len(results)} biens retenus après filtres")
    return results


async def _scrape_detail(
    client: httpx.AsyncClient, dept: str, url: str, titre: str, desc_liste: str
) -> dict | None:
    r = await client.get(url)
    if r.status_code != 200:
        return None
    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    # Description : contenu détaillé si dispo, sinon celle de la liste
    description = desc_liste
    body = soup.select_one(".elementor-widget-theme-post-content, .entry-content, article")
    if body:
        txt = body.get_text(" ", strip=True)
        if len(txt) > len(description):
            description = txt

    full_text = f"{titre} {description}"

    prix = _parse_price(_field(html, "Prix"))
    surface = _parse_surface(_field(html, "Surface")) or _parse_surface(full_text)
    ref = _parse_ref(html, url)
    dpe = _parse_dpe(html)

    surface_terrain = _parse_terrain(full_text)
    pieces = _first_int(r"(\d+)\s*pi[eè]ces", full_text)
    chambres = _first_int(r"(\d+)\s*chambres?", full_text)

    ville, code_postal = _parse_ville_cp(full_text)
    # Type déduit du TITRE en priorité (le corps détaillé contient du boilerplate
    # « château… » de l'agence qui fausserait la détection).
    type_bien = _guess_type(titre) or _guess_type(description) or "propriété"

    photos = _parse_photos(html)

    return {
        "source": "chateauxetpatrimoine",
        "url": url,
        "id_annonce": ref,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": (ville or "")[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Châteaux et Patrimoine",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _field(html: str, label: str) -> str:
    """Récupère la valeur après 'Label : ...' jusqu'au prochain tag/fin."""
    m = re.search(rf">\s*{label}\s*:?\s*([^<]{{1,40}})<", html)
    return m.group(1).strip() if m else ""


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text)
    if not cleaned:
        return None
    try:
        val = float(cleaned)
        return val if val >= 1000 else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'Surface : 330 m²' ou '... de 330 m² habitables ...' → 330.0"""
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0]*)\s*m²", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
        return f if 8 <= f <= 5000 else None
    except ValueError:
        return None


def _parse_terrain(text: str) -> float | None:
    """Cherche un terrain en hectares (NNha NNa NNca) ou en m²."""
    if not text:
        return None
    # Format cadastral : 'sur 69ha 95a 38ca' → m²
    m = re.search(
        r"(\d+)\s*ha(?:\s*(\d+)\s*a)?(?:\s*(\d+)\s*ca)?", text, re.IGNORECASE
    )
    if m:
        ha = int(m.group(1))
        a = int(m.group(2)) if m.group(2) else 0
        ca = int(m.group(3)) if m.group(3) else 0
        return float(ha * 10000 + a * 100 + ca)
    # Sinon 'terrain de NNNN m²'
    m = re.search(r"terrain[^0-9]{0,15}(\d[\d\s\xa0]*)\s*m²", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            return float(val)
        except ValueError:
            pass
    return None


def _parse_dpe(html: str) -> str | None:
    """'DPE : 271 F   GES : 85 F' → 'F'  (lettre de la conso, pas du GES)."""
    m = re.search(r"DPE\s*:?\s*(\d+)?\s*([A-G])\b", html)
    if m:
        return m.group(2).upper()
    # DPE en cours / vierge → None
    return None


def _parse_ref(html: str, url: str) -> str:
    m = re.search(r">\s*Ref[^<:]*:?\s*([A-Za-z0-9-]+)\s*<", html)
    if m:
        return m.group(1).strip()
    # secours : slug 'ref-2153' dans l'URL
    m = re.search(r"ref-([a-z0-9]+)/?$", url)
    return m.group(1) if m else url


def _first_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _parse_ville_cp(text: str) -> tuple[str | None, str]:
    """CP rarement présent ; ville devinée (province/commune au mieux)."""
    cp = ""
    m = re.search(r"\b(\d{5})\b", text)
    if m:
        cp = m.group(1)
    ville = None
    # motif 'commune de Xxx' / 'à Xxx (' — best-effort, jamais éliminatoire
    m = re.search(r"(?:commune de|ville de|à)\s+([A-ZÉÈÀ][\w'\-]+(?:[ -][A-ZÉÈÀ][\w'\-]+){0,2})", text)
    if m:
        ville = m.group(1).strip()
    return ville, cp


def _guess_type(text: str) -> str | None:
    low = (text or "").lower()
    for t in ("château", "chateau", "manoir", "moulin", "demeure", "domaine",
              "longère", "longere", "gentilhommière", "bastide", "chartreuse",
              "maison de maître", "propriété", "propriete", "haras", "ferme",
              "maison"):
        if t in low:
            return t.replace("chateau", "château").replace("longere", "longère")
    return None


def _parse_photos(html: str) -> list[str]:
    imgs = re.findall(
        r"(https://www\.chateauxetpatrimoine\.com/wp-content/uploads/20\d\d/\d\d/[^\"'?\s)]+\.(?:jpg|jpeg|png))",
        html,
        re.IGNORECASE,
    )
    out: list[str] = []
    seen: set[str] = set()
    for src in imgs:
        low = src.lower()
        if "logo" in low or "fnaim" in low or "-150x" in low or "icon" in low:
            continue
        # déduplique sur la base sans suffixe de redimensionnement (-300x225)
        base = re.sub(r"-\d+x\d+(\.\w+)$", r"\1", src)
        if base in seen:
            continue
        seen.add(base)
        out.append(src)
        if len(out) >= PHOTOS_PER_BIEN:
            break
    return out


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
    print(f"\nTotal Châteaux et Patrimoine : {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens})
    print(f"Départements vus : {depts}")
    cp_depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus (via CP) : {cp_depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — DPE {b.get('dpe')}"
            f" — {b['type_bien']} — {len(b['photos'])} photos"
        )

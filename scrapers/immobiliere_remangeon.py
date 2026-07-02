"""scrapers/immobiliere_remangeon.py — Immobilière Remangeon (agence régionale indépendante, depuis 1946)

Méthode : scrape_simple (httpx) — SSR HTML (plateforme Webgenery, cdn.webgenery.net)
Segment  : Sologne / Brenne / vallée du Cher — propriétés, demeures de caractère,
           fermes et terrains. Bureaux Lamotte-Beuvron + Vierzon.
           Couvre principalement 41 / 18 / 36 (+ quelques biens 45).

URL pattern : /fr/ventes/{N}  (pagination ~7 pages, 12 cartes/page)
              → PAS de filtre département serveur fiable : on scrape toute la
                liste et on POST-FILTRE sur code_postal[:2] in departements.

ATTENTION : les pages détail /fr/vente/.../{id} font un 301 vers /fr/ventes —
            toutes les données doivent être extraites des pages liste.

Cartes : div.card-liste
  - URL/lien   : a.card-content[href]  → /fr/vente/{slug-cp}/{id}
                 (les biens vendus sont en /fr/bien-vendu/... → exclus)
  - Photo lien : a.img_bien[href], img[src] = {cdn}/{compte}/taille3/{id}-1.jpg
                 (on reconstruit {id}-N.jpg jusqu'à nb_photo)
  - Titre      : h3.card-titre  → "Demeure de prestige 7 pièces à la vente 45240 - Sennely"
                 → type + nb pièces ; span.cp = CP, span.commune = ville
  - Accroche   : p.accroche (description, surface parfois dedans)
  - Prix       : p.price span.prix  → "795 000 € HAI"
  - Référence  : span.reference  → "Réf : IR716"
  - nb photos  : span.nb_photo

Filtre dept : post-filtre STRICT code_postal[:2]. Le CP vient de span.cp et, en
              secours (cartes vendues sans span.cp), du suffixe du slug d'URL
              (-45240/, -18100/, -41300/).

Surface habitable / terrain / chambres / DPE : absents des cartes → parsés depuis
l'accroche si possible, sinon None.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.immobiliere-remangeon.fr"
MAX_PAGES = 12
PHOTOS_PER_CARD = 10


# Types de bien (mots de titre) à conserver : maisons / propriétés / fermes / demeures...
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|ferme|long[èe]re|manoir|ch[âa]teau|moulin|"
    r"demeure|domaine|mas|g[îi]te|corps de ferme|fermette|grange|pavillon|"
    r"terrain|chasse|[ée]tang",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|local|commerce|garage|parking|immeuble|bureau|fonds|"
    r"murs|entrep[ôo]t|hangar",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/fr/ventes/{page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Remangeon] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("div.card-liste")
            if not cards:
                break

            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                # Post-filtre département STRICT (0 fuite)
                cp = bien["code_postal"]
                if not cp or cp[:2] not in departements:
                    continue
                bien["departement"] = cp[:2]

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

            await asyncio.sleep(0.5)

    # Comptage par département (info)
    counts: dict[str, int] = {}
    for b in results:
        counts[b["departement"]] = counts.get(b["departement"], 0) + 1
    for d in sorted(counts):
        print(f"[Remangeon] Dept {d}: {counts[d]} annonces")

    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a.card-content") or card.select_one("a.img_bien")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    # Exclure les biens vendus (/fr/bien-vendu/...)
    if "/bien-vendu/" in href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # id_annonce : dernier segment d'URL
    parts = [p for p in href.split("/") if p]
    id_url = parts[-1] if parts else ""

    # CP / ville : span.cp + span.commune, secours via suffixe du slug d'URL
    h3 = card.select_one("h3.card-titre")
    cp = ""
    ville = ""
    if h3:
        cp_el = h3.select_one("span.cp")
        if cp_el:
            cp = cp_el.get_text(strip=True)
        ville_el = h3.select_one("span.commune")
        if ville_el:
            ville = ville_el.get_text(strip=True)
    if not cp:
        m_cp = re.search(r"-(\d{5})/", href + "/")
        if m_cp:
            cp = m_cp.group(1)
    cp = cp if re.fullmatch(r"\d{5}", cp or "") else ""

    # Titre (type + pièces) : texte du h3 hors spans cp/commune
    titre = ""
    type_bien = "maison"
    pieces = None
    if h3:
        # Récupère le texte avant les spans (ex: "Demeure de prestige 7 pièces à la vente")
        head = ""
        for node in h3.contents:
            if getattr(node, "name", None) == "span":
                break
            if getattr(node, "name", None) == "br":
                break
            head += str(node.string or "") if not getattr(node, "name", None) else ""
        head = re.sub(r"\s+", " ", head).strip()
        head = re.sub(r"\s*à la vente\s*$", "", head, flags=re.IGNORECASE).strip()
        titre = head
        # Type = head sans le nb de pièces
        type_clean = re.sub(r"\d+\s*pi[èe]ces?", "", head, flags=re.IGNORECASE).strip()
        if type_clean:
            type_bien = type_clean.lower()
        m_p = re.search(r"(\d+)\s*pi[èe]ces?", head, re.IGNORECASE)
        if m_p:
            pieces = int(m_p.group(1))

    if not titre:
        titre = f"{type_bien} {ville}".strip()
    full_titre = f"{titre} {ville} ({cp})".strip() if cp else titre

    # Filtre type : on écarte les biens clairement hors-cible
    if _EXCLUDE_TYPE.search(type_bien) and not _KEEP_TYPE.search(type_bien):
        return None

    # Description (accroche)
    acc_el = card.select_one("p.accroche")
    description = acc_el.get_text(" ", strip=True) if acc_el else ""

    # Prix
    price_el = card.select_one("p.price span.prix")
    prix = _parse_price(price_el.get_text(" ", strip=True) if price_el else "")

    # Référence
    ref_el = card.select_one("span.reference")
    ref = ""
    if ref_el:
        ref = re.sub(r"^R[ée]f\s*:?\s*", "", ref_el.get_text(strip=True), flags=re.IGNORECASE)
    id_annonce = ref or id_url or url

    # Surface habitable depuis l'accroche
    surface = _parse_surface_hab(description) or _parse_surface_hab(titre)
    surface_terrain = _parse_terrain(description)

    # Photos : reconstruction depuis {cdn}/{compte}/tailleX/{id}-1.jpg
    photos: list[str] = []
    img = card.select_one("a.img_bien img") or card.select_one("img")
    base_src = img.get("src") or img.get("data-src") if img else ""
    nb_photo = 1
    nb_el = card.select_one("span.nb_photo")
    if nb_el:
        m_nb = re.search(r"(\d+)", nb_el.get_text(strip=True))
        if m_nb:
            nb_photo = int(m_nb.group(1))
    if base_src and base_src.startswith("http"):
        m = re.match(r"(.*?)-(\d+)\.(jpg|jpeg|png)$", base_src, re.IGNORECASE)
        if m:
            stem, ext = m.group(1), m.group(3)
            n = min(nb_photo, PHOTOS_PER_CARD)
            photos = [f"{stem}-{i}.{ext}" for i in range(1, n + 1)]
        else:
            photos = [base_src]

    return {
        "source": "immobiliere_remangeon",
        "url": url,
        "id_annonce": id_annonce,
        "titre": full_titre[:150],
        "type_bien": type_bien[:60] or "maison",
        "description": description[:1200],
        "departement": cp[:2] if cp else "",
        "ville": ville[:80],
        "code_postal": cp,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Immobilière Remangeon",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", re.sub(r"\s|\xa0", "", text))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_terrain(text: str) -> float | None:
    """Cherche un terrain/étang/parc en ha ou m² dans l'accroche."""
    if not text:
        return None
    # hectares
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*ha\b", text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(",", ".")) * 10000
        except ValueError:
            pass
    m = re.search(
        r"terrain[^0-9]{0,15}(\d[\d\s\xa0]*)\s*m[²2]", text, re.IGNORECASE
    )
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if f >= 50:
                return f
        except ValueError:
            pass
    return None


def _parse_surface_hab(text: str) -> float | None:
    """Cherche 'NNN m²' d'habitation dans le texte libre (accroche/titre)."""
    if not text:
        return None
    m = re.search(
        r"(?:habitation|maison|habitable|surface)[^0-9]{0,20}(\d[\d\s\xa0]*)\s*m[²2]",
        text,
        re.IGNORECASE,
    )
    if not m:
        m = re.search(r"de\s+(\d[\d\s\xa0]*)\s*m[²2]", text, re.IGNORECASE)
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
    print(f"\nTotal Immobilière Remangeon: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b.get('pieces') or '?'}p — {b['type_bien']} — {b['ville']}"
            f" — {len(b['photos'])} photos"
        )

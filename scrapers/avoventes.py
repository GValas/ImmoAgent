"""scrapers/avoventes.py — Avoventes (ventes aux enchères publiques immobilières)

Plateforme nationale du Conseil National des Barreaux : annonces de ventes aux
enchères immobilières (saisies / adjudications judiciaires) publiées par les
cabinets d'avocats avant audience au tribunal judiciaire.

Méthode : scrape_simple (httpx) — SSR HTML pur (serveur nginx, pas de Cloudflare).
URL : /recherche  → page UNIQUE qui liste TOUTES les annonces nationales
      (~220-230 lots, pas de pagination). On scrape une fois puis on POST-FILTRE
      par code postal[:2] (le formulaire de filtre dept n'expose pas d'URL stable,
      le scrape national + post-filtre est plus fiable et 0 fuite).

Cartes : div[data-link]   (data-link = URL page détail /enchere/{slug})
  - Type    : 2ᵉ span.badge (le 1ᵉʳ est "Vente aux enchères")
  - Titre   : div.font-bold
  - Adresse : div.inline-block  →  "2 Rue de la Roche, 45160 Olivet, France"
              → code postal + ville extraits par regex
  - Prix    : "Mise à prix : <strong>50 000,00 €</strong>"
  - Cabinet : "Cabinet : <strong>SOREL & ASSOCIES</strong>"  → agence
  - Photo   : style inline  background: url( ... )  (souvent /img/noimg.png → ignorée)

Filtre département : POST-FILTRE strict code_postal[:2] ∈ departements (0 fuite).
Stock cible : faible mais réel (ex. 36, 45, 58, 89 ont des lots ; varie au gré
              des audiences à venir).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://avoventes.fr"
SEARCH_URL = f"{BASE_URL}/recherche"
PHOTOS_PER_CARD = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types de bien à conserver (badge) : maisons / propriétés. On exclut le reste.
_KEEP_TYPE = re.compile(
    r"maison|propri[eé]t[eé]|villa|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|mas|g[iî]te|corps de ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local|commerce|garage|parking|immeuble|bureau|"
    r"fonds|cave|box|emplacement",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
    ) as client:
        try:
            r = await client.get(SEARCH_URL)
        except Exception as e:
            print(f"[Avoventes] Erreur requête: {e}")
            return results

        if r.status_code != 200:
            print(f"[Avoventes] Statut HTTP {r.status_code}")
            return results

        cards = BeautifulSoup(r.text, "html.parser").select("div[data-link]")
        print(f"[Avoventes] {len(cards)} lots nationaux")

        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue

            # POST-FILTRE département STRICT (0 fuite hors-zone)
            cp = bien.get("code_postal") or ""
            if not cp or cp[:2] not in departements:
                continue
            bien["departement"] = cp[:2]

            aid = bien["id_annonce"]
            if aid in seen_ids:
                continue

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            # NB : "prix" = mise à prix (enchère) = prix PLANCHER volontairement
            # bas (décote 20-40 %), pas la valeur de marché. On n'applique donc
            # PAS prix_min ici (il viderait la source à tort) ; on garde prix_max
            # par sécurité contre un lot manifestement hors budget.
            if prix_max and p and p > prix_max:
                continue
            if surface_min and s and s < surface_min:
                continue

            seen_ids.add(aid)
            results.append(bien)

    # Récap par département
    par_dept: dict[str, int] = {}
    for b in results:
        par_dept[b["departement"]] = par_dept.get(b["departement"], 0) + 1
    for d in sorted(par_dept):
        print(f"[Avoventes] Dept {d}: {par_dept[d]} lots")

    return results


def _parse_card(card) -> dict | None:
    href = card.get("data-link", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Type de bien : 2ᵉ badge (1ᵉʳ = "Vente aux enchères")
    badges = [b.get_text(strip=True) for b in card.select("span.badge")]
    type_label = ""
    for b in badges:
        if "enchère" in b.lower() or "enchere" in b.lower():
            continue
        type_label = b
        break
    if _EXCLUDE_TYPE.search(type_label):
        return None
    if not _KEEP_TYPE.search(type_label):
        return None
    type_bien = type_label.lower().strip() or "maison"

    # Titre
    title_el = card.select_one("div.font-bold")
    titre = title_el.get_text(" ", strip=True) if title_el else ""

    # Adresse : "... , 45160 Olivet, France"
    adresse = ""
    for d in card.select("div.inline-block"):
        txt = d.get_text(" ", strip=True)
        if re.search(r"\b\d{5}\b", txt):
            adresse = txt
            break
    ville, code_postal = _parse_adresse(adresse)
    if not titre:
        titre = f"{type_bien.title()} {ville}".strip()

    # Texte complet de la carte pour prix / cabinet
    card_text = card.get_text(" ", strip=True)

    # Prix (mise à prix)
    prix = None
    m_prix = re.search(r"Mise à prix\s*:?\s*([\d\s\xa0.,]+)\s*€", card_text)
    if m_prix:
        prix = _parse_price(m_prix.group(1))

    # Cabinet (agence)
    agence = "Avoventes"
    m_cab = re.search(r"Cabinet\s*:?\s*([^|\n]+?)(?:Mise à prix|Date|$)", card_text)
    if m_cab:
        cab = m_cab.group(1).strip(" .-")
        if cab:
            agence = cab[:80]

    # Photo (background-image inline)
    photos = []
    for m in re.findall(r"url\(\s*([^)\s]+)\s*\)", str(card)):
        src = m.strip("'\"")
        if src and "noimg" not in src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    # id_annonce : dernier segment du slug
    slug = [p for p in href.split("/") if p]
    id_annonce = slug[-1] if slug else url

    return {
        "source": "avoventes",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": "",
        "departement": code_postal[:2] if code_postal else "",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": None,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": agence,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_adresse(text: str) -> tuple[str, str]:
    """'2 Rue de la Roche, 45160 Olivet, France' → ('Olivet', '45160')"""
    if not text:
        return "", ""
    cp = ""
    ville = ""
    m = re.search(r"\b(\d{5})\s+([A-Za-zÀ-ÿ'’\-\s]+?)(?:,|$)", text)
    if m:
        cp = m.group(1)
        ville = m.group(2).strip()
        # purge "France" si collé
        ville = re.sub(r"\s+France\s*$", "", ville, flags=re.IGNORECASE).strip()
    return ville, cp


def _parse_price(text: str) -> float | None:
    """'50 000,00 €' → 50000.0"""
    cleaned = re.sub(r"[€\s\xa0]", "", text)
    # virgule décimale française → on supprime la partie centimes ,00
    cleaned = re.sub(r",\d{2}$", "", cleaned)
    cleaned = re.sub(r"[^\d]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
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
    print(f"\nTotal Avoventes: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b['type_bien']} — {b['ville']} — {b['agence']}"
        )

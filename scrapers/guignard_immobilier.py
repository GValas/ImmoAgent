"""scrapers/guignard_immobilier.py — L'Immobilière Guignard (Indre 36 / Cher 18)

Agence indépendante du Berry (Châteauroux, Bourges, Argenton-sur-Creuse) couvrant
l'Indre (36) et le Cher (18). Site SSR (CMS « Webgenery ») : toutes les annonces
sont dans le HTML brut → httpx pur.

Méthode : scrape_simple (httpx) — SSR HTML
URL pattern : /fr/acheter/maison/all/all/all/all/{page}   (page = 1..MAX_PAGES)
              (page 1 = /fr/acheter/maison)
              → pas de filtre dept côté serveur ici (on prend tout le stock maison
                de l'agence, qui mêle 36 et 18). Filtre dept = POST-FILTRE STRICT
                sur le CP, présent dans l'URL détail ET dans .cp.

Filtre département (0 fuite) :
  - chaque carte expose .cp (ex. "36170") et son lien détail
    /fr/vente/maison-{N}-pieces-{ville}-{CP}/{uuid} contient aussi le CP ;
  - on retient le CP de .cp (recoupé avec celui du lien) et on n'accepte la carte
    que si CP[:2] ∈ départements cibles.

Cartes : article.fiches-immo  (data-uuid)
  - URL    : a.img_bien[href]  → /fr/vente/maison-{N}-pieces-{ville}-{CP}/{uuid}
  - CP     : .cp
  - Ville  : .commune
  - Prix   : .prix  → "Prix de vente : 34 900 €"
  - Réf    : .reference  → "Réf. : 5678"
  - DPE    : .info_bulle (lettre seule, hors boutons localiser/diaporama/sélection)
  - Pièces : déduit du slug URL "maison-{N}-pieces-..."
  - Surface: extraite de la description JSON-LD (Product) embarquée dans la carte
  - Photos : cdn.webgenery.net/.../{uuid}-N.jpg (img + span[data-fancybox-href])

Type de bien : la rubrique scrappée est « maison » (filtre serveur sur le segment
               d'URL) ; on revérifie tout de même côté client.

Couverture : Indre (36) + Cher (18).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import json
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.limmobiliere-guignard.com"
LIST_PATH_P1 = "/fr/acheter/maison"
LIST_PATH_PN = "/fr/acheter/maison/all/all/all/all/{page}"
MAX_PAGES = 12
PHOTOS_PER_CARD = 8


_KEEP_TYPE = re.compile(
    r"maison|villa|propri[eé]t[eé]|ferme|long[eè]re|manoir|ch[aâ]teau|moulin|"
    r"demeure|domaine|mas|g[iî]te|corps de ferme|maison de village|pavillon|"
    r"bourg|campagne|b[aâ]tisse",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|local commercial|garage|parking|bureau|"
    r"fonds|hangar|studio",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            path = LIST_PATH_P1 if page == 1 else LIST_PATH_PN.format(page=page)
            url = f"{BASE_URL}{path}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[GuignardImmo] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = BeautifulSoup(r.text, "html.parser").select("article.fiches-immo")
            if not cards:
                break

            new_ids = 0
            for card in cards:
                try:
                    bien = _parse_card(card)
                except Exception:
                    continue
                if not bien:
                    continue

                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                new_ids += 1

                # POST-FILTRE STRICT — 0 fuite hors-zone
                cp = bien["code_postal"]
                if not cp or cp[:2] not in departements:
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

            if new_ids == 0:
                break

            await asyncio.sleep(0.5)

    print(f"[GuignardImmo] {len(results)} annonces (depts {sorted({b['departement'] for b in results}) or '∅'})")
    return results


def _parse_card(card) -> dict | None:
    link = card.select_one("a.img_bien") or card.select_one("a[href*='/fr/vente/']")
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else f"{BASE_URL}{href}"

    uuid = card.get("data-uuid") or ""

    # Type + pièces depuis le slug : maison-{N}-pieces-{ville}-{CP}
    slug = href.rsplit("/", 1)[0].split("/")[-1] if "/" in href else href
    type_slug = slug.split("-")[0] if slug else "maison"
    if _EXCLUDE_TYPE.search(slug) and not _KEEP_TYPE.search(slug):
        return None
    type_bien = _deduce_type(slug) or "maison"
    pieces = None
    m_p = re.search(r"-(\d+)-pieces?-", slug)
    if m_p:
        pieces = int(m_p.group(1))

    # CP : .cp (recoupé avec le lien)
    cp_el = card.select_one(".cp")
    cp = cp_el.get_text(strip=True) if cp_el else ""
    cp = re.sub(r"\D", "", cp)[:5]
    m_cp = re.search(r"-(\d{5})/", href) or re.search(r"-(\d{5})$", slug)
    cp_url = m_cp.group(1) if m_cp else ""
    if cp and cp_url and cp != cp_url:
        return None  # divergence → prudence anti-fuite
    code_postal = cp or cp_url
    if not code_postal:
        return None

    commune_el = card.select_one(".commune")
    ville = commune_el.get_text(" ", strip=True) if commune_el else ""

    # Prix : "Prix de vente : 34 900 €"
    prix_el = card.select_one(".prix")
    prix = _parse_price(prix_el.get_text(" ", strip=True) if prix_el else "")

    # Référence
    ref_el = card.select_one(".reference")
    ref_raw = ref_el.get_text(" ", strip=True) if ref_el else ""
    ref = re.sub(r"^.*?:\s*", "", ref_raw).strip()
    id_annonce = ref or uuid or url

    # Surface + description via JSON-LD (Product) embarqué
    surface = None
    description = ""
    ld = card.find("script", type="application/ld+json")
    if ld and ld.string:
        surface, description = _from_jsonld(ld.string)
    if surface is None:
        surface = _surface_from_text(card.get_text(" ", strip=True))

    titre = f"{type_bien.title()} {pieces or ''} pièces {ville}".replace("  ", " ").strip()

    # DPE : .info_bulle qui contient une lettre A–G seule
    dpe = None
    for ib in card.select(".info_bulle"):
        if ib.get("class") and any(
            c in ("localiserSite", "imgDetailA", "selectionBien")
            for c in ib.get("class")
        ):
            continue
        t = ib.get_text(strip=True)
        if re.fullmatch(r"[A-G]", t or ""):
            dpe = t
            break

    # Photos : cdn.webgenery.net/.../{uuid}-N.jpg
    photos = []
    for img in card.select("a.img_bien img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(src)
    for sp in card.select("[data-fancybox-href]"):
        src = sp.get("data-fancybox-href") or ""
        if src and src not in photos:
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "guignard_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "L'Immobilière Guignard",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _deduce_type(text: str) -> str:
    m = _KEEP_TYPE.search(text or "")
    return m.group(0).lower() if m else ""


def _from_jsonld(raw: str) -> tuple[float | None, str]:
    try:
        data = json.loads(raw)
    except Exception:
        return None, ""
    graph = data.get("@graph", [data]) if isinstance(data, dict) else data
    desc = ""
    for node in graph if isinstance(graph, list) else [graph]:
        if isinstance(node, dict) and node.get("@type") == "Product":
            desc = node.get("description", "") or desc
    surface = _surface_from_text(desc)
    return surface, desc


def _surface_from_text(text: str) -> float | None:
    """Surface HABITABLE seulement (prudence : on n'accepte qu'une mention
    explicite "habitable", sinon None — éviter de confondre avec le terrain ou la
    taille d'une pièce, ce qui fausserait le filtre surface_min)."""
    if not text:
        return None
    for pat in (
        r"(\d+(?:[.,]\d+)?)\s*m²?\s*(?:hab\.?|habitables?)",
        r"habitables?[^0-9]{0,15}(\d+(?:[.,]\d+)?)\s*m²?",
        r"surface\s+habitable[^0-9]{0,15}(\d+(?:[.,]\d+)?)\s*m²?",
    ):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                f = float(m.group(1).replace(",", "."))
                if 8 <= f <= 2000:
                    return f
            except ValueError:
                pass
    return None


def _parse_price(text: str) -> float | None:
    m = re.search(r"([\d\s\xa0]+)\s*€", text)
    if not m:
        return None
    raw = re.sub(r"[^\d]", "", m.group(1))
    try:
        v = float(raw) if raw else None
    except ValueError:
        return None
    if v and v < 1000:
        return None
    return v


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
    print(f"\nTotal Immobilière Guignard: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['pieces'] or '?'}p"
            f" — DPE {b['dpe'] or '?'} — {b['ville']}"
        )

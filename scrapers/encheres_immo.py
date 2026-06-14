"""scrapers/encheres_immo.py — Enchères Immo (encheres-immo.com)

Plateforme d'enchères immobilières interactives en ligne (ventes par appels d'offres,
animées par des professionnels). Couverture NATIONALE.

Méthode : scrape_simple (httpx) — SSR HTML (Phoenix LiveView, mais le rendu initial
contient déjà toutes les cartes ; httpx suffit, pas de Playwright).
URL pattern : /annonces   (rendu unique ~30 biens ; la pagination se fait par
              websocket LiveView, non adressable en httpx → on prend l'inventaire rendu).

Cartes : article > a[href="/annonce/{type}-{N}-pieces-{surface}m2-{ville}-{cp}-{id}"]
  Le slug d'URL encode TYPE, pièces, surface, ville, CODE POSTAL et id → tout est
  extractible côté liste, plus le texte de la carte (prix, ville-CP, pièces, chambres).

Filtre DÉPARTEMENT : pas de filtre serveur exploitable en httpx. On parse l'inventaire
  national rendu puis on POST-FILTRE STRICT par le CP du slug (code_postal[:2]).
  → 0 fuite garantie.

`prix` = prix de l'enchère en cours (prix de départ / dernière offre).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://encheres-immo.com"
LISTING_PATH = "/annonces"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_KEEP_TYPE = re.compile(
    r"maison|villa|propri[ée]t[ée]|ferme|long[èe]re|manoir|chateau|ch[âa]teau|"
    r"moulin|demeure|domaine|mas|corps[- ]de[- ]ferme|hotel|h[ôo]tel",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|garage|parking|immeuble|local|commerce|bureau|"
    r"fonds|cave|box|studio|loft",
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
        try:
            r = await client.get(BASE_URL + LISTING_PATH)
        except Exception as e:
            print(f"[EncheresImmo] ERR: {e}")
            return results
        if r.status_code != 200:
            print(f"[EncheresImmo] status {r.status_code}")
            return results

        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("article a[href^='/annonce/']")
        for a in cards:
            try:
                bien = _parse_card(a)
            except Exception:
                continue
            if not bien:
                continue

            aid = bien["id_annonce"]
            if aid in seen_ids:
                continue
            seen_ids.add(aid)

            # POST-FILTRE département STRICT
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

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print(
        f"[EncheresImmo] total: {len(results)} biens (zone cible) — par dept: {by_dept}"
    )
    return results


def _parse_card(a) -> dict | None:
    href = a.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    slug = href.rsplit("/", 1)[-1]
    # slug : {type}-{N}-pieces-{surface}m2-{ville}-{cp}-{id}
    m_id = re.search(r"-(\d+)$", slug)
    id_annonce = m_id.group(1) if m_id else url

    # Code postal : dernier groupe de 5 chiffres avant l'id final
    code_postal = ""
    m_cp = re.search(r"-(\d{5})-\d+$", slug)
    if m_cp:
        code_postal = m_cp.group(1)
    if not code_postal:
        # secours : depuis le texte de la carte "Ville - 81330"
        m2 = re.search(r"\b(\d{5})\b", a.get_text(" ", strip=True))
        if m2:
            code_postal = m2.group(1)
    dept = code_postal[:2] if code_postal else ""

    # Type de bien : début du slug
    type_seg = slug.split("-")[0]
    if _EXCLUDE_TYPE.search(type_seg) and not _KEEP_TYPE.search(type_seg):
        return None
    if not _KEEP_TYPE.search(type_seg):
        return None
    type_bien = type_seg

    full = a.get_text(" ", strip=True)

    # Titre : <p class="text-lg font-semibold">
    title_el = a.select_one("p.text-lg, p.font-semibold")
    titre = title_el.get_text(" ", strip=True) if title_el else ""
    if not titre:
        titre = f"{type_bien.title()} {code_postal}".strip()

    # Ville : texte après le marqueur localisation "Ville - 81330"
    ville = ""
    m_v = re.search(r"([A-Za-zÀ-ÿ' \-]+?)\s*-\s*\d{5}", full)
    if m_v:
        ville = m_v.group(1).strip()

    # Prix : "99 000 €"
    prix = None
    m_p = re.search(r"([\d][\d\s\xa0]{2,})\s*€", full)
    if m_p:
        prix = _parse_price(m_p.group(1))

    # Surface depuis le slug "-136-80m2-" ou le titre "136,80m2"
    surface = None
    m_s = re.search(r"-((?:\d+-)?\d+)m2-", slug)
    if m_s:
        try:
            surface = float(m_s.group(1).replace("-", "."))
        except ValueError:
            surface = None
    if surface is None:
        m_s2 = re.search(r"(\d+(?:[.,]\d+)?)\s*m2", full)
        if m_s2:
            try:
                surface = float(m_s2.group(1).replace(",", "."))
            except ValueError:
                surface = None

    # Pièces & chambres depuis le texte
    pieces = None
    m_pc = re.search(r"(\d+)\s*pi[eè]ces?", full)
    if m_pc:
        pieces = int(m_pc.group(1))
    elif (m2 := re.search(r"-(\d+)-pieces-", slug)):
        pieces = int(m2.group(1))
    chambres = None
    m_ch = re.search(r"(\d+)\s*chambres?", full)
    if m_ch:
        chambres = int(m_ch.group(1))

    # Photo
    photos = []
    img = a.find("img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            photos.append(src)

    return {
        "source": "encheres_immo",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien[:60],
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Enchères Immo",
    }


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

    async def _test():
        depts = ["72", "28", "45", "89", "49", "37", "36", "18", "58", "41", "53"]
        biens = await search(
            {"departements": depts, "prix_max": 0, "prix_min": 0, "surface_min": 0}
        )
        print(f"\nTotal Enchères Immo (zone): {len(biens)} biens")
        depts_vus = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
        print(f"Départements vus : {depts_vus}")
        for b in biens[:10]:
            print(
                f"  [{b['code_postal']}] {b['titre'][:48]} — {b['prix']}€"
                f" — {b.get('surface') or '?'}m² — {b.get('pieces') or '?'}p — {b['ville']}"
            )

    asyncio.run(_test())

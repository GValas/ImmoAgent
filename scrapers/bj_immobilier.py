"""scrapers/bj_immobilier.py — Breton & Jeanneau Immobilier (Sarthe & Mayenne)

Méthode : scrape_simple (httpx) — SSR (CMS maison "c-mos").

Agence implantée en Sarthe (72) et Mayenne (53). Filtre département CÔTÉ SERVEUR
fiable via le slug d'URL :
    /fr/achat-immobilier/maisons/departement-{slug}   (ex: departement-sarthe)
→ 0 fuite hors-dept (vérifié : 72 = 14 cartes 100% 72 ; 53 = 15 cartes 100% 53).
La pagination ?p=N n'est pas servie en httpx (AJAX) → ~14-15 maisons/dept (cap réel).

Cartes : div.c-mos__bien
  - Titre  : .c-mos__bien__titre
  - Lieu   : .c-mos__bien__lieu  →  "Bien immobilier en Vente à Ville (CP)"
  - Prix   : .c-mos__bien__prix  →  "336 400 € **"
  - URL    : a[href*='/fr/acheter/{slug}_{id}']
  - Photo  : img (//www.bj-immobilier.fr/upimg/...)

Surface / chambres / DPE : absents des cartes → récupérés sur la fiche détail
(JSON-LD RealEstateListing + texte "Surface habitable : NN m²"), concurrence limitée.

Couverture cible : 72 et 53 uniquement (les autres slugs depts = 0 stock).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.bj-immobilier.fr"
PHOTOS_PER_CARD = 8
DETAIL_CONCURRENCY = 6


# Seuls les départements où l'agence a des biens
DEPT_SLUGS: dict[str, str] = {
    "72": "sarthe",
    "53": "mayenne",
}


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for dept in departements:
            slug = DEPT_SLUGS.get(dept)
            if not slug:
                continue
            try:
                biens = await _scrape_dept(client, dept, slug)
                # Enrichir les fiches en parallèle (surface/dpe/desc)
                await _enrich(client, biens)
                # Filtres critères (après enrichissement surface)
                kept = []
                for b in biens:
                    p = b.get("prix") or 0
                    s = b.get("surface") or 0
                    if prix_max and p and p > prix_max:
                        continue
                    if prix_min and p and p < prix_min:
                        continue
                    if surface_min and s and s < surface_min:
                        continue
                    kept.append(b)
                results.extend(kept)
                print(f"[BJ] Dept {dept}: {len(kept)} annonces")
            except Exception as e:
                print(f"[BJ] Erreur dept {dept}: {e}")
            await asyncio.sleep(0.4)

    return results


async def _scrape_dept(client: httpx.AsyncClient, dept: str, slug: str) -> list[dict]:
    url = f"{BASE_URL}/fr/achat-immobilier/maisons/departement-{slug}"
    r = await client.get(url)
    if r.status_code != 200:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    biens: list[dict] = []
    seen: set[str] = set()

    for card in soup.select("div.c-mos__bien"):
        bien = _parse_card(card, dept)
        if not bien:
            continue
        if bien["id_annonce"] in seen:
            continue
        # Sécurité : on n'accepte que le département cible
        if bien["code_postal"] and bien["code_postal"][:2] != dept:
            continue
        seen.add(bien["id_annonce"])
        biens.append(bien)

    return biens


def _parse_card(card, dept: str) -> dict | None:
    link = card.find("a", href=re.compile(r"/fr/acheter/[^\"']+_\d+"))
    if not link:
        return None
    href = link["href"]
    url = href if href.startswith("http") else BASE_URL + href
    m_id = re.search(r"_(\d+)(?:[/?#]|$)", href)
    id_annonce = m_id.group(1) if m_id else url

    titre_el = card.select_one(".c-mos__bien__titre")
    titre = titre_el.get_text(" ", strip=True) if titre_el else ""

    lieu_el = card.select_one(".c-mos__bien__lieu")
    lieu = lieu_el.get_text(" ", strip=True) if lieu_el else ""
    ville, code_postal = _parse_loc(lieu)

    prix_el = card.select_one(".c-mos__bien__prix") or card.select_one(
        ".c-mos__bien__tarif"
    )
    prix = _parse_price(prix_el.get_text(" ", strip=True) if prix_el else "")

    photos = []
    for img in card.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original") or ""
        if "/upimg/" in src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            if src not in photos:
                photos.append(src)

    if not titre:
        titre = f"Maison {ville}".strip()

    # Exclure bureaux / commerces / terrains / appartements (réseau mixte)
    if re.search(
        r"\bbureaux?\b|\blocal\b|commerce|\bterrain\b|appartement|\bimmeuble\b|"
        r"garage|parking|fonds de commerce",
        titre,
        re.IGNORECASE,
    ):
        return None

    return {
        "source": "bj_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": _detect_type(titre),
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": None,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos[:PHOTOS_PER_CARD],
        "dpe": None,
        "agence": "Breton & Jeanneau Immobilier",
    }


async def _enrich(client: httpx.AsyncClient, biens: list[dict]) -> None:
    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def one(b):
        async with sem:
            try:
                r = await client.get(b["url"])
                if r.status_code != 200:
                    return
                _parse_detail(r.text, b)
            except Exception:
                pass

    await asyncio.gather(*(one(b) for b in biens))


def _parse_detail(html: str, b: dict) -> None:
    soup = BeautifulSoup(html, "html.parser")
    txt = soup.get_text(" ", strip=True)

    m = re.search(r"Surface\s+habitable\s*:?\s*([\d\s\xa0]+)\s*m", txt, re.IGNORECASE)
    if not m:
        m = re.search(r"([\d\s\xa0]{2,})\s*m²\s*habitable", txt, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 3000:
                b["surface"] = f
        except ValueError:
            pass

    m_t = re.search(r"Surface\s+(?:du\s+)?terrain\s*:?\s*([\d\s\xa0]+)\s*m", txt, re.IGNORECASE)
    if m_t:
        val = re.sub(r"[\s\xa0]", "", m_t.group(1))
        try:
            b["surface_terrain"] = float(val)
        except ValueError:
            pass

    m_ch = re.search(r"(\d+)\s*chambres?", txt, re.IGNORECASE)
    if m_ch:
        b["chambres"] = int(m_ch.group(1))
    m_p = re.search(r"(\d+)\s*pi[eè]ces?", txt, re.IGNORECASE)
    if m_p:
        b["pieces"] = int(m_p.group(1))

    # DPE : lettre classe énergie (évite de confondre avec le GES)
    m_dpe = re.search(r"(?:DPE|Classe\s+[ée]nerg[ée]tique)[^A-G]{0,30}\b([A-G])\b", txt)
    if m_dpe:
        b["dpe"] = m_dpe.group(1).upper()

    # Description depuis le JSON-LD si présent
    md = re.search(r'"@type"\s*:\s*"RealEstateListing".*?"description"\s*:\s*"(.*?)"', html, re.DOTALL)
    if md:
        desc = md.group(1).encode().decode("unicode_escape", errors="ignore")
        b["description"] = re.sub(r"\s+", " ", desc).strip()[:1200]

    # Photos pleine taille
    imgs = re.findall(r'(//www\.bj-immobilier\.fr/upimg/[^\s"\']+/normals/[^\s"\']+\.jpg)', html)
    seen = list(b["photos"])
    for src in imgs:
        full = "https:" + src
        if full not in seen:
            seen.append(full)
    if seen:
        b["photos"] = seen[:PHOTOS_PER_CARD]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _detect_type(titre: str) -> str:
    t = titre.lower()
    for kw in ("château", "chateau", "manoir", "moulin", "longère", "longere",
               "ferme", "villa", "propriété", "propriete", "demeure"):
        if kw in t:
            return kw.replace("chateau", "château").replace("propriete", "propriété")
    return "maison"


def _parse_loc(text: str) -> tuple[str, str]:
    """'Bien immobilier en Vente à Rouesse vasse (72140)' → ('Rouesse Vasse', '72140')"""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text)
    if m_cp:
        cp = m_cp.group(1)
    m_ville = re.search(r"\b[àa]\s+(.+?)\s*\(\d{5}\)", text)
    ville = m_ville.group(1).strip() if m_ville else ""
    return ville.title(), cp


def _parse_price(text: str) -> float | None:
    m = re.search(r"([\d][\d\s\xa0]{2,})\s*€", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1))
    try:
        f = float(val)
        return f if f > 1000 else None
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
    print(f"\nTotal Breton & Jeanneau: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b.get('chambres') or '?'}ch"
            f" — DPE {b.get('dpe') or '?'} — {b['ville']}"
        )

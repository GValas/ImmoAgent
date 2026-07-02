"""scrapers/declic_immo.py — Déclic Immo (réseau de mandataires / adaptimmo CMS)

Méthode : scrape_simple (httpx) — SSR HTML (CMS Adaptimmo, pages .html/.htm).

Pas de filtre département serveur exploitable :
  - /fr/acheter.htm  → 404
  - le formulaire /fr/liste.htm filtre par localité (autocomplete JS) ou tracé carte,
    pas par code département en querystring simple.
  - les listings par commune (/fr/annonces/{ville}-p-r301-0-{idVille}-1.html) exigent
    de connaître l'identifiant interne de chaque ville (impraticable pour un département).
Donc on scrape le LISTING NATIONAL (/fr/annonces-immobilieres-p-r12-{N}.html,
~12 biens/page, ~35 pages, ~412 biens au total) et on POST-FILTRE par département.

Filtre département : la RÉFÉRENCE produit du bien (segment final de l'URL fiche
…-p-r7-{REF}.html) commence par le code département sur 2 chiffres
(ex: 3700418725 → 37, 1700617103 → 17). Vérifié contre le code postal de la
fiche détail (LUZE (37120) ↔ ref 3700…) : aucune fuite. On valide aussi en
seconde barrière le code_postal[:2] extrait de la fiche détail.

Cartes liste : div.liste-bien-container
  - URL/réf : a[href*="-p-r7-"]  → REF dans …-p-r7-{REF}.html
  - type    : .liste-bien-type   (Maison, Propriété, Longere, Villa, Terrain…)
  - ville   : .liste-bien-ville
  - prix    : .liste-bien-price  ("Prix : 126 000 €*")
Fiche détail (pour CP / surface / pièces, absents de la liste) :
  - ville+CP : h2.detail-bien-ville  → "LUZE (37120)"
  - specs    : .detail-bien-specs ul li  → "75 m²", "5 pièce(s)", "3 chambre(s)", "940 m²"
  - desc     : [itemprop=description]

Couverture réelle (2026-05-30) : inventaire national concentré sur le Sud-Ouest /
littoral (17, 16, 34, 85, 06, 79, 37, 33, 86, 81). Sur les 11 départements cibles
du projet, SEUL le 37 (Indre-et-Loire) a du stock (~33 biens, dont quelques maisons).
Tous les autres depts cibles : 0 bien.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.declic.immo"
LISTING_URL = f"{BASE_URL}/fr/annonces-immobilieres-p-r12-{{page}}.html"
MAX_PAGES = 45            # plafond de sécurité (~35 pages réelles)
DETAIL_CONCURRENCY = 4
PHOTOS_PER_CARD = 1


# Types de bien (libellé .liste-bien-type) à conserver : maisons / propriétés…
_KEEP_TYPE = re.compile(
    r"maison|propri[ée]t[ée]|villa|longere|longère|manoir|chateau|château|"
    r"ferme|grange|moulin|demeure|domaine|mas|pavillon|gite|gîte|haras",
    re.IGNORECASE,
)
# Types explicitement exclus
_EXCLUDE_TYPE = re.compile(
    r"appartement|studio|duplex|terrain|local|locaux|commerce|cave|garage|"
    r"parking|immeuble|bureau|fonds|bar|caf[ée]|restaurant|snack|pizzeria|"
    r"boulangerie|salon|institut|r[ée]sidence|[ée]tang",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1) listing national → cartes brutes filtrées par dept (via ref) + type
        candidates = await _fetch_candidates(client, departements)

        # 2) enrichissement fiche détail (CP, surface, pièces) en parallèle limité
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def enrich(cand):
            async with sem:
                return await _enrich_detail(client, cand)

        biens = await asyncio.gather(*(enrich(c) for c in candidates))

    results: list[dict] = []
    seen: set[str] = set()
    for bien in biens:
        if not bien:
            continue

        # Seconde barrière anti-fuite : code_postal[:2] de la fiche détail
        cp = bien.get("code_postal") or ""
        dept_cp = cp[:2] if len(cp) >= 2 else ""
        dept_ref = (bien.get("id_annonce") or "")[:2]
        dept = dept_cp or dept_ref
        if departements and dept not in departements:
            continue
        # si on a un CP et qu'il contredit le dept ciblé → on jette (fuite)
        if dept_cp and departements and dept_cp not in departements:
            continue
        bien["departement"] = dept

        p = bien.get("prix") or 0
        s = bien.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue

        aid = bien.get("id_annonce") or bien.get("url")
        if aid in seen:
            continue
        seen.add(aid)
        results.append(bien)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[DeclicImmo] Dept {dept}: {n} annonces")

    return results


async def _fetch_candidates(
    client: httpx.AsyncClient, departements: list[str]
) -> list[dict]:
    """Parcourt le listing national, garde les cartes maison/propriété dont la
    référence (préfixe dept) tombe dans les départements ciblés."""
    candidates: list[dict] = []
    seen_ref: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = LISTING_URL.format(page=page)
        try:
            r = await client.get(url)
        except Exception as e:
            print(f"[DeclicImmo] Erreur page {page}: {e}")
            break
        if r.status_code != 200:
            break

        soup = BeautifulSoup(r.content, "html.parser")
        cards = soup.select("div.liste-bien-container")
        if not cards:
            break

        for card in cards:
            cand = _parse_card(card)
            if not cand:
                continue
            ref = cand["id_annonce"]
            if ref in seen_ref:
                continue
            # filtre dept via préfixe de la référence
            if departements and ref[:2] not in departements:
                continue
            # filtre type
            tp = cand["type_bien"]
            if _EXCLUDE_TYPE.search(tp) and not _KEEP_TYPE.search(tp):
                continue
            if not _KEEP_TYPE.search(tp):
                continue
            seen_ref.add(ref)
            candidates.append(cand)

        # dernière page : pas de lien vers page+1
        if not soup.select_one(f'a[href*="-p-r12-{page + 1}.html"]'):
            break

        await asyncio.sleep(0.4)

    return candidates


def _parse_card(card) -> dict | None:
    a = card.select_one("a[href*='-p-r7-']")
    if not a or not a.get("href"):
        return None
    href = a["href"].strip()
    url = href if href.startswith("http") else BASE_URL + href
    m = re.search(r"-p-r7-(\d+)\.html", href)
    if not m:
        return None
    ref = m.group(1)

    def txt(sel):
        e = card.select_one(sel)
        return e.get_text(" ", strip=True) if e else ""

    type_bien = (txt(".liste-bien-type") or "").strip()
    ville = (txt(".liste-bien-ville") or "").strip()
    prix = _parse_num(txt(".liste-bien-price"))

    # photo de couverture
    photos = []
    img = card.select_one("img.vedette_image")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src.startswith("http"):
            photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "declic_immo",
        "url": url,
        "id_annonce": ref,
        "titre": (f"{type_bien} {ville}".strip() or "Maison")[:150],
        "type_bien": (type_bien or "maison").lower(),
        "description": None,
        "departement": ref[:2],
        "ville": ville.title()[:80] if ville else None,
        "code_postal": None,
        "surface": None,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "dpe": None,
        "photos": photos,
        "agence": "Déclic Immo",
    }


async def _enrich_detail(client: httpx.AsyncClient, cand: dict) -> dict | None:
    """Récupère CP / surface habitable / pièces / chambres / terrain sur la fiche."""
    try:
        r = await client.get(cand["url"])
        if r.status_code != 200:
            # on garde la carte (dept connu via ref) même sans détail
            return cand
        soup = BeautifulSoup(r.content, "html.parser")

        # ville + CP : "LUZE (37120)"
        ville_el = soup.select_one("h2.detail-bien-ville")
        if ville_el:
            loc = ville_el.get_text(" ", strip=True)
            m_cp = re.search(r"\((\d{5})\)", loc)
            if m_cp:
                cand["code_postal"] = m_cp.group(1)
            v = re.sub(r"\s*\(\d{5}\)\s*$", "", loc).strip()
            if v:
                cand["ville"] = v.title()[:80]

        # specs : ul li → "75 m²", "5 pièce(s)", "3 chambre(s)", "940 m²"
        specs = soup.select(".detail-bien-specs li")
        surfaces_m2 = []
        for li in specs:
            t = li.get_text(" ", strip=True)
            if re.search(r"pi[èe]ce", t, re.IGNORECASE):
                mm = re.search(r"(\d+)", t)
                if mm:
                    cand["pieces"] = int(mm.group(1))
            elif re.search(r"chambre", t, re.IGNORECASE):
                mm = re.search(r"(\d+)", t)
                if mm:
                    cand["chambres"] = int(mm.group(1))
            elif "m²" in t:
                val = _parse_num(t)
                if val:
                    surfaces_m2.append(val)
        # 1ère surface m² = habitable ; dernière (si >1) = terrain
        if surfaces_m2:
            cand["surface"] = surfaces_m2[0]
            if len(surfaces_m2) >= 2:
                cand["surface_terrain"] = surfaces_m2[-1]

        # prix (fiche, plus fiable)
        prix_el = soup.select_one(".detail-bien-prix")
        if prix_el:
            p = _parse_num(prix_el.get_text(" ", strip=True))
            if p:
                cand["prix"] = p

        # description
        desc_el = soup.select_one("[itemprop=description]")
        if desc_el:
            cand["description"] = desc_el.get_text(" ", strip=True)[:1200]

        # type plus précis depuis h1
        type_el = soup.select_one("h1.detail-bien-type")
        if type_el:
            t = type_el.get_text(" ", strip=True)
            if t:
                cand["type_bien"] = t.lower()
                cand["titre"] = f"{t} {cand['ville'] or ''}".strip()[:150]

        # photos détail (galerie)
        gallery = []
        for img in soup.select("img[src*='photos-biens'], img[data-src*='photos-biens']"):
            src = img.get("src") or img.get("data-src") or ""
            if src.startswith("http"):
                gallery.append(src.split("?")[0])
        if gallery:
            seen = []
            for s in gallery:
                if s not in seen:
                    seen.append(s)
            cand["photos"] = seen[:10]

        await asyncio.sleep(0.2)
    except Exception:
        pass
    return cand


def _parse_num(text: str) -> float | None:
    """'126 000 €*' / '75 m²' / '6 000 €' → float."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ")
    cleaned = re.sub(r"[^\d,\. ]", "", cleaned).replace(" ", "")
    cleaned = cleaned.replace(",", ".")
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


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
    print(f"\nTotal Déclic Immo (depts cibles): {len(biens)} annonces")
    depts = sorted({(b["code_postal"] or b["id_annonce"])[:2] for b in biens})
    print(f"Départements vus : {depts}")
    for b in biens[:15]:
        print(
            f"  [{b.get('code_postal') or b['id_annonce'][:2]+'???'}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

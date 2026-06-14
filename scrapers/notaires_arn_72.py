"""scrapers/notaires_arn_72.py — Alliance Réseau Notaires (arn.notaires.fr)

Réseau d'offices notariaux du Grand-Lucé / Sud-Sarthe (SAS Alliance Réseau Notaires).
Cœur d'activité : la Sarthe (72) — quasi-totalité du stock en 72.

Méthode : scrape_simple (httpx) — SSR HTML (gabarit Realfusio/immonot, contenu dans le
HTML brut, pas de JS).
URL pattern : /annonces-immobilieres-sarthe.html  (page liste unique, pas de pagination
              réelle — petit office). PAS de filtre département serviable côté serveur.
              On récupère tout puis POST-FILTRE STRICT par le nom de département du slug.

Cartes : div.bloc-annonce-carre
  - Lien   : a[href -> /annonces/detail/{id}/key/{k}/vente-{type}-{dept-slug}-{ville}.html]
             → le slug encode TYPE, NOM DE DÉPARTEMENT et ville.
  - Type   : .col-md-8  "Vente Maison"
  - Prix   : .text-right "168 000 €"  (charge acquéreur / FAI)
  - Ville  : .light-color "Le Grand-Lucé"
  - Surface: .col-md-4 "118 m2"
  - Réf    : "Réf. 13816/976"

Filtre DÉPARTEMENT : le NOM de département est dans le slug (…-sarthe-…, …-mayenne-…).
  On le mappe vers le code et on POST-FILTRE STRICT sur la zone cible (le CP exact n'est
  pas dans la carte — il l'est en page détail, récupéré ensuite par gallery.py).
  → 0 fuite : un bien hors-zone (ex. …-loire-atlantique-…) est rejeté.

Couverture (juin 2026) : ~9 annonces, ~4 maisons, toutes en Sarthe (72) — petit volume
mais réel, complémentaire des autres sources notariales 72 (notaires_rnc_sarthe,
notaires_lccbn_sarthe). Distinct domaine/office.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.arn.notaires.fr"
LISTING = f"{BASE_URL}/annonces-immobilieres-sarthe.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Nom de département (slug) → code, pour les 11 départements cibles.
DEPT_NAME_TO_CODE = {
    "sarthe": "72",
    "eure-et-loir": "28",
    "loiret": "45",
    "yonne": "89",
    "maine-et-loire": "49",
    "indre-et-loire": "37",
    "indre": "36",
    "cher": "18",
    "nievre": "58",
    "loir-et-cher": "41",
    "mayenne": "53",
}

_KEEP_TYPE = re.compile(
    r"maison|villa|propri[ée]t[ée]|ferme|long[èe]re|manoir|chateau|ch[âa]teau|"
    r"moulin|demeure|domaine|mas|corps[- ]de[- ]ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|garage|parking|immeuble|local|commerce|bureau|"
    r"fonds|cave|box|studio|murs",
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
            r = await client.get(LISTING)
        except Exception as e:
            print(f"[NotairesARN] ERR: {e}")
            return results
        if r.status_code != 200:
            print(f"[NotairesARN] status {r.status_code}")
            return results

        cards = BeautifulSoup(r.text, "html.parser").select(".bloc-annonce-carre")
        for card in cards:
            try:
                bien = _parse_card(card)
            except Exception:
                continue
            if not bien:
                continue

            # POST-FILTRE département STRICT (via nom de dept du slug)
            if bien["departement"] not in departements:
                continue

            aid = bien["id_annonce"]
            if aid in seen_ids:
                continue
            seen_ids.add(aid)

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
    print(f"[NotairesARN] total: {len(results)} biens (zone cible) — par dept: {by_dept}")
    return results


def _parse_card(card) -> dict | None:
    link = card.find("a", href=True)
    href = link.get("href", "") if link else ""
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    slug = href.rsplit("/", 1)[-1].replace(".html", "")

    # id depuis le href : /annonces/detail/{ID}__{key}/...
    m_id = re.search(r"/detail/(\w+)", href)
    id_annonce = m_id.group(1) if m_id else url

    # Département : nom dans le slug (le plus long nom matché)
    dept = ""
    for name in sorted(DEPT_NAME_TO_CODE, key=len, reverse=True):
        if f"-{name}-" in f"-{slug}-":
            dept = DEPT_NAME_TO_CODE[name]
            break
    if not dept:
        return None

    # Type de bien
    type_el = card.select_one(".col-md-8") or card.select_one(".col-sm-8")
    type_txt = type_el.get_text(" ", strip=True) if type_el else ""
    type_txt = re.sub(r"^\s*Vente\s*", "", type_txt).strip()
    if not type_txt:
        m = re.search(r"vente-([a-z\-]+?)-(?:" + "|".join(DEPT_NAME_TO_CODE) + r")-", slug)
        if m:
            type_txt = m.group(1).replace("-", " ")
    if _EXCLUDE_TYPE.search(type_txt) and not _KEEP_TYPE.search(type_txt):
        return None
    if not _KEEP_TYPE.search(type_txt):
        return None
    type_bien = type_txt.lower()

    # Ville
    ville_el = card.select_one(".light-color")
    ville = ville_el.get_text(" ", strip=True) if ville_el else ""

    full = card.get_text(" ", strip=True)

    # Prix (charge acquéreur, 1er montant € important)
    prix = None
    m_pr = re.search(r"([\d][\d\s\xa0]{2,})\s*€", full)
    if m_pr:
        prix = _parse_price(m_pr.group(1))

    # Surface "110.59 m²" / "118 m2"
    surface = None
    m_s = re.search(r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m[²2]\b", full)
    if m_s:
        try:
            surface = float(re.sub(r"[\s\xa0]", "", m_s.group(1)).replace(",", "."))
            if not (8 <= surface <= 5000):
                surface = None
        except ValueError:
            surface = None

    # Photo
    photos = []
    img = card.find("img")
    if img:
        src = img.get("src") or ""
        if src and not src.startswith("data:") and "marianne" not in src:
            photos.append(src if src.startswith("http") else BASE_URL + src)

    titre = f"{type_bien.title()} à {ville}".strip() if ville else type_bien.title()

    return {
        "source": "notaires_arn_72",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien[:60],
        "description": "",
        "departement": dept,
        "ville": ville[:80],
        "code_postal": "",  # exact CP récupéré en page détail (gallery.py)
        "surface": surface,
        "surface_terrain": None,
        "pieces": None,
        "chambres": None,
        "prix": prix,
        "photos": photos[:1],
        "dpe": None,
        "agence": "Alliance Réseau Notaires (Sarthe)",
    }


def _parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        val = float(cleaned) if cleaned else None
        return val if (val and val >= 1000) else None
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
        print(f"\nTotal Notaires ARN: {len(biens)} biens")
        print("Départements vus :", sorted({b["departement"] for b in biens}))
        for b in biens[:10]:
            print(
                f"  [{b['departement']}] {b['titre'][:50]} — {b['prix']}€"
                f" — {b.get('surface') or '?'}m² — {b['ville']}"
            )

    asyncio.run(_test())

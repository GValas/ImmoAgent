"""scrapers/viager_europe.py — Viager Europe (viager-europe.com)

Réseau national spécialisé en viager (occupé / libre / nue-propriété). SSR HTML
(WordPress / Oxygen builder, contenu dans le HTML brut — httpx pur, pas de Playwright).

Méthode : scrape_simple (httpx).
URL pattern : /annonces/page/{N}/   (~20 cartes/page, pagination /page/N/).

Cartes : chaque fiche se repère par son lien « Voir annonce » a.an_voir_annonce
  (href -> www.viager-europe.com/annonces-details/{secteur}/{type-viager}/{ville}/{REF}).
  La carte (parent direct du lien) porte un bloc texte unique :
    "MANDELIEU LA NAPOULE - 06210 | Appartement | 2 pièces | Prix d'achat 126 000 € |
     Décote 50% | Valeur du bien 250 000 € | Bouquet (FAI) 99 500 € | Rente 250 €/mois | 78 ans"
  → VILLE + CODE POSTAL, type, pièces, bouquet/prix d'achat, valeur du bien, rente,
    décote, âge du crédirentier sont tous extractibles côté liste.

Filtre DÉPARTEMENT : pas de filtre serveur fiable → on scrape l'inventaire national
  (pagination) et on POST-FILTRE STRICT par code_postal[:2]. → 0 fuite garantie.

`prix` = VALEUR DU BIEN (valeur vénale) pour rester comparable aux autres sources ;
le bouquet / la rente / la décote sont reportés en description. 100 % viager → le mot
"viager" sera présent (utile/attendu selon les mots_interdits de l'utilisateur).

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://viager-europe.com"
LISTING_PATH = "/annonces"
MAX_PAGES = 20  # garde-fou

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
    r"moulin|demeure|domaine|mas|pavillon|corps[- ]de[- ]ferme",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain|garage|parking|immeuble|local|commerce|bureau|"
    r"fonds|cave|box|studio|loft|appt",
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
            url = (
                f"{BASE_URL}{LISTING_PATH}/"
                if page == 1
                else f"{BASE_URL}{LISTING_PATH}/page/{page}/"
            )
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[ViagerEurope] ERR page {page}: {e}")
                break
            if r.status_code != 200:
                break

            soup = BeautifulSoup(r.text, "html.parser")
            links = soup.select("a.an_voir_annonce") or soup.select(
                ".an_voir_annonce a"
            )
            if not links:
                break

            new_on_page = 0
            for link in links:
                try:
                    bien = _parse_card(link)
                except Exception:
                    continue
                if not bien:
                    continue
                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                new_on_page += 1

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

            if new_on_page == 0:
                break
            await asyncio.sleep(0.5)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    print(f"[ViagerEurope] total: {len(results)} biens (zone cible) — par dept: {by_dept}")
    return results


def _parse_card(link) -> dict | None:
    href = link.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    # Réf depuis le slug : .../{ville}/{REF}
    ref = href.rstrip("/").rsplit("/", 1)[-1]
    id_annonce = ref or url

    # Carte = plus proche ancêtre du lien "Voir annonce" qui porte un code postal
    card = link
    full = ""
    for _ in range(6):
        if card.parent is None:
            break
        card = card.parent
        txt = card.get_text(" ", strip=True)
        if re.search(r"\b\d{5}\b", txt):
            full = txt
            break
    if not full:
        return None

    # Ville + code postal : "MANDELIEU LA NAPOULE - 06210"
    code_postal = ""
    ville = ""
    m_loc = re.search(r"([A-Za-zÀ-ÿ' \-]+?)\s*-\s*(\d{5})\b", full)
    if m_loc:
        ville = m_loc.group(1).strip()
        code_postal = m_loc.group(2)
    if not code_postal:
        return None
    dept = code_postal[:2]

    # Type de bien : mot après le bloc localisation
    type_bien = ""
    m_t = _KEEP_TYPE.search(full)
    m_excl = _EXCLUDE_TYPE.search(full)
    if m_t and (not m_excl or m_t.start() < m_excl.start()):
        type_bien = m_t.group(0).lower()
    elif m_excl:
        return None  # appartement / studio → exclu
    else:
        return None  # type non identifié → prudence

    # Pièces
    pieces = None
    m_p = re.search(r"(\d+)\s*pi[eè]ces?", full)
    if m_p:
        pieces = int(m_p.group(1))

    # Prix = "Valeur du bien : 250 000 €" (valeur vénale). Repli : "Prix d'achat".
    prix = None
    m_val = re.search(r"Valeur du bien\D*([\d\s\xa0]+)\s*€", full)
    if m_val:
        prix = _parse_price(m_val.group(1))
    if prix is None:
        m_pa = re.search(r"Prix d['’]achat\D*([\d\s\xa0]+)\s*€", full)
        if m_pa:
            prix = _parse_price(m_pa.group(1))

    # Description : on consolide les infos viager
    desc_parts = []
    for label, pat in [
        ("Type", r"(Viager Occup[ée]|Viager Libre|Nue[- ]propri[ée]t[ée]|Vente [àa] terme)"),
        ("Bouquet", r"Bouquet[^\d]*([\d\s\xa0]+€)"),
        ("Rente", r"Rente[^\d]*([\d\s\xa0]+€)[^/]*/mois"),
        ("Décote", r"D[ée]cote[^\d]*(\d+%)"),
        ("Crédirentier", r"(\d+)\s*ans"),
    ]:
        m = re.search(pat, full, re.IGNORECASE)
        if m:
            desc_parts.append(f"{label} : {m.group(1).strip()}")
    description = " — ".join(desc_parts)

    # Photo (background-image ou img dans la carte / sa parente)
    photos = []
    holder = card
    for _ in range(3):
        img = holder.select_one("img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src and not src.startswith("data:"):
                photos.append(src if src.startswith("http") else BASE_URL + src)
                break
        if holder.parent:
            holder = holder.parent

    titre = f"{type_bien.title()} viager à {ville}".strip()

    return {
        "source": "viager_europe",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien[:60],
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": None,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos[:1],
        "dpe": None,
        "agence": "Viager Europe",
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
        print(f"\nTotal Viager Europe (zone): {len(biens)} biens")
        depts_vus = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
        print(f"Départements vus : {depts_vus}")
        for b in biens[:10]:
            print(
                f"  [{b['code_postal']}] {b['titre'][:48]} — {b['prix']}€"
                f" — {b.get('pieces') or '?'}p — {b['ville']} — {b['description'][:50]}"
            )

    asyncio.run(_test())

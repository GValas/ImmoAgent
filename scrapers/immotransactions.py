"""scrapers/immotransactions.py — Immo Transactions (Avallon / Saulieu)

Agence locale de l'Auxois-Morvan-Avallonnais (https://www.immotransactions.fr),
site Joomla + composant AdsManager. Couvre à la fois l'YONNE (89, Avallon — zone
cible) ET la CÔTE-D'OR (21, Saulieu — HORS ZONE, à exclure).

Méthode : scrape_simple (httpx) — SSR HTML.
URL liste paginée : /nos-biens-en-vente.html?start=N  (AdsManager pagine par
tranche de 60 via ?start ; 2 pages couvrent ~95 biens). Pagination retenue :
boucle ?start=0,60,120… jusqu'à page vide ou page déjà vue.

Cartes : <table class="adsmanager_table"> → lignes
<tr class="adsmanager_table_description trcategory_N">, chacune avec un
<h4 class="no-margin-top"> contenant <a href="/nos-biens-en-vente/{cat}/{id}-{slug}.html">.
La catégorie est dans le href (4-maisons = résidentiel à garder ; 3-terrains,
5-appartements, 9/10-locaux, 11/1-divers = exclus selon leur nature).

Filtre département (0 fuite EXIGÉE) : la carte liste ne porte PAS de code postal.
On extrait des NOMS DE COMMUNE candidats depuis le titre/slug, on les résout en
(dept, cp) via scrapers._geo_resolver.resolve_communes restreint aux départements
cibles. Une commune hors-cible (ex. Saulieu→21) renvoie (None, None) → le bien est
écarté. Les biens sans commune identifiable (« Maison ancienne à conforter »…)
sont aussi écartés par prudence (prudence > fuite).

Interface : async def search(criteres: dict) -> list[dict]
"""
from __future__ import annotations

import asyncio
import re

from bs4 import BeautifulSoup

from scrapers._base import (
    get_with_retry,
    make_client,
    parse_int,
    parse_price,
    parse_terrain,
    standalone_main,
)
from scrapers._geo_resolver import _norm, resolve_communes

BASE_URL = "https://www.immotransactions.fr"
LIST_URL = BASE_URL + "/nos-biens-en-vente.html"
PAGE_SIZE = 60
MAX_PAGES = 6  # garde-fou (≈95 biens tiennent en 2 pages)

# Catégories AdsManager (préfixe du href /nos-biens-en-vente/{cat}/) à conserver
# (résidentiel). Les locaux/terrains/garages/immeubles/appartements sont écartés.
_KEEP_CAT = re.compile(r"^\d+-(maisons?|propriete|propri[ée]t[ée]s?|villas?)", re.IGNORECASE)
_EXCLUDE_CAT = re.compile(
    r"^\d+-(terrains?|locaux|appartements?|fonds|garages?|immeubles?|parkings?|"
    r"commerces?)",
    re.IGNORECASE,
)

# Mots du titre/slug à ne JAMAIS prendre pour une commune (faux candidats).
_STOP = {
    "maison", "maisons", "appartement", "propriete", "propriété", "terrain",
    "immeuble", "grange", "longere", "longère", "fermette", "ferme", "moulin",
    "chalet", "chalets", "garage", "local", "commerce", "etang", "étang",
    "parc", "naturel", "morvan", "auxois", "bourgogne", "secteur", "proche",
    "centre", "ville", "village", "ancienne", "ancien", "charmante", "charmant",
    "belle", "bel", "grande", "grand", "petite", "petit", "magnifique", "superbe",
    "agreable", "agréable", "coquette", "coeur", "cœur", "rare", "exclusivite",
    "exclusivité", "opportunite", "opportunité", "exceptionnelle", "isolee",
    "isolée", "isole", "habitation", "renover", "rénover", "restaurer",
    "conforter", "rafraichir", "réhabilitée", "rehabilitee", "caractere",
    "caractère", "campagne", "logements", "logement", "usage", "mixte",
    "commercial", "atelier", "artiste", "rapport", "louee", "louée", "jardin",
    "dependances", "dépendances", "dependance", "dépendance", "vue", "degagee",
    "dégagée", "piscine", "pieds", "pied", "plain", "lac", "settons", "seine",
    "cure", "basilique", "historique", "pierres", "pierre", "granit", "pays",
    "rénové", "renove", "nichée", "nichee", "ecrin", "écrin", "verdure",
    "calme", "lumineux", "habitable", "habitables", "exceptionnel",
    "remarquable", "plus", "bon", "thil", "forges",
    "autun", "joli", "jolie", "confortable", "charme", "garages", "travaux", "sans", "deux",
    "trois", "rénovée", "renovee", "entierement",
    "entièrement", "situee", "située", "minutes", "kilometres", "kilomètres",
    "un", "une", "ha", "hectares", "hectare", "corps", "parcelle",
    "vente",
}

# Prépositions introduisant un nom de lieu : on capture ce qui suit.
# Mots de liaison/parasites courts qui ne doivent jamais former un candidat
# seuls (sinon le geo API matche des communes homonymes « Du », « Le », « À »…).
_LIAISON = {
    "de", "du", "des", "le", "la", "les", "et", "a", "au", "aux", "en", "sur",
    "sous", "par", "pour", "dans", "un", "une", "d", "l", "ou", "ses", "son",
    "sa", "lès", "m", "c", "n", "s", "mn", "km", "ha", "av", "no",
}


def _is_name_token(tok: str) -> bool:
    """Un token « nom de lieu » : Capitalisé ou MAJUSCULE, ≥2 lettres, hors stop."""
    n = _norm(tok)
    if not n or n in _STOP or n in _LIAISON or len(n) < 2:
        return False
    # un token composé uniquement de mots de liaison (« d'un », « de la ») n'est
    # pas une commune (évite « D'UN » → commune homonyme « Dun »).
    sub = [p for p in re.split(r"[ '’\-]", n) if p]
    if sub and all(p in _LIAISON for p in sub):
        return False
    # doit commencer par une majuscule (les descriptifs « maison » sont en minuscules)
    return bool(re.match(r"^[A-ZÀ-Þ]", tok))


# Prépositions de lieu : la commune suit généralement (« sur GUILLON »,
# « proche de CUSSY », « secteur Avallon », « entre SAULIEU et AVALLON »,
# « à SAULIEU »). On n'extrait une commune QUE si elle est ancrée ainsi ou en
# tête de titre — ce qui évite que des mots descriptifs majuscules au milieu du
# titre (« CHARME », « BON », « PLUS », « REMARQUABLE ») soient pris pour des
# communes homonymes par l'API geo.
_LOC_PREP = {
    "sur", "a", "à", "proche", "pres", "près", "secteur", "entre", "vers",
}


def _runs_from_words(words: list[str]) -> list[str]:
    """Découpe une suite de mots en runs de tokens-noms (liaisons internes OK)."""
    runs: list[str] = []
    run: list[str] = []
    for w in words:
        if _is_name_token(w):
            run.append(w)
        elif run and _norm(w) in _LIAISON:
            run.append(w)
        else:
            if run:
                runs.append(run)
                run = []
    if run:
        runs.append(run)
    out: list[str] = []
    for r in runs:
        while r and _norm(r[0]) in _LIAISON:
            r = r[1:]
        while r and _norm(r[-1]) in _LIAISON:
            r = r[:-1]
        if r and any(_is_name_token(x) for x in r):
            out.append(" ".join(r))
    return out


def _ville_candidates(titre: str, slug: str) -> list[str]:
    """Noms de commune plausibles, ANCRÉS (tête de titre ou après préposition de
    lieu). Pour chaque ancrage on émet le run complet (commune composée) PUIS
    chaque token-nom isolé, dans l'ordre — la résolution prend le 1ᵉʳ candidat
    tombant dans un département cible. Ancrer évite les faux matchs sur les mots
    descriptifs majuscules en milieu de titre.
    """
    cands: list[str] = []

    def _push(g: str):
        n = _norm(g)
        if n and len(n) >= 3 and n not in {_norm(c) for c in cands}:
            cands.append(g)

    def _emit(frag_words: list[str]):
        for run in _runs_from_words(frag_words):
            _push(run)
            for w in run.split():
                if _is_name_token(w):
                    _push(w)

    toks = [t.strip(".,;:()'’\"") for t in (titre or "").split()]

    # 1) Ancrage « tête de titre » : la commune ouvre souvent l'annonce.
    if toks and _is_name_token(toks[0]):
        # run initial (avant le 1er mot non-nom/non-liaison)
        lead: list[str] = []
        for t in toks:
            if _is_name_token(t) or (lead and _norm(t) in _LIAISON):
                lead.append(t)
            else:
                break
        _emit(lead)

    # 2) Ancrage « après préposition de lieu » : on prend les ≤4 mots suivants.
    for i, t in enumerate(toks):
        if _norm(t) in _LOC_PREP:
            _emit(toks[i + 1: i + 5])

    return cands


async def _fetch_rows(client) -> list:
    """Récupère toutes les lignes d'annonce en paginant via ?start=N."""
    rows: list = []
    seen_pages: set[str] = set()
    for page in range(MAX_PAGES):
        start = page * PAGE_SIZE
        url = LIST_URL if start == 0 else f"{LIST_URL}?start={start}"
        r = await get_with_retry(client, url)
        if r is None or r.status_code != 200:
            break
        page_rows = BeautifulSoup(r.text, "html.parser").select(
            "tr.adsmanager_table_description"
        )
        if not page_rows:
            break
        # signature de page pour stopper si AdsManager renvoie la même liste
        sig = "|".join(
            (rw.select_one("a[href]").get("href", "") if rw.select_one("a[href]") else "")
            for rw in page_rows[:3]
        )
        if sig in seen_pages:
            break
        seen_pages.add(sig)
        rows.extend(page_rows)
        if len(page_rows) < PAGE_SIZE:
            break
        await asyncio.sleep(0.5)
    return rows


def _parse_row(row) -> dict | None:
    """Transforme une ligne en bien partiel (sans dept/cp encore résolus)."""
    h4 = row.select_one("h4.no-margin-top") or row.select_one("h4")
    link = h4.select_one("a[href]") if h4 else row.select_one("a[href]")
    href = link.get("href", "") if link else ""
    if not href:
        return None

    # catégorie depuis le 2e segment du href : /nos-biens-en-vente/{cat}/{id}-...
    parts = [p for p in href.split("?")[0].split("/") if p]
    cat = ""
    for p in parts:
        if re.match(r"^\d+-", p) and "biens-en-vente" not in p:
            cat = p
            break
    if _EXCLUDE_CAT.search(cat):
        return None
    if not _KEEP_CAT.search(cat):
        return None  # catégorie inconnue/ambiguë → exclue par prudence

    last = parts[-1].split(".html")[0] if parts else ""
    m_id = re.match(r"^(\d+)-(.*)$", last)
    id_annonce = m_id.group(1) if m_id else last
    slug = m_id.group(2) if m_id else last

    type_bien = "maison"
    if re.search(r"propri", cat, re.IGNORECASE):
        type_bien = "propriété"
    elif re.search(r"villa", cat, re.IGNORECASE):
        type_bien = "villa"

    url = href if href.startswith("http") else BASE_URL + href
    titre = link.get_text(" ", strip=True) if link else ""
    row_text = row.get_text(" ", strip=True)

    m_price = re.search(r"([\d][\d\s\xa0]{2,})\s*€", row_text)
    prix = parse_price(m_price.group(1)) if m_price else None

    surface = parse_int(r"(\d[\d\s\xa0]*)\s*m²?\s*(?:hab|habitable)", row_text)
    surface = float(surface) if surface else None
    surface_terrain = parse_terrain(row_text)
    pieces = parse_int(r"(\d+)\s*pi[èe]ces?", row_text)
    chambres = parse_int(r"(\d+)\s*chambres?", row_text)

    photos = []
    img = row.select_one("img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            photos.append(src)

    return {
        "source": "immotransactions",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": row_text[:1200],
        "departement": None,
        "ville": None,
        "code_postal": None,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Immo Transactions",
        # champ interne (retiré avant retour) : candidats-ville à résoudre
        "_cands": _ville_candidates(titre, slug),
    }


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    depts_set = set(departements)
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    async with make_client() as client:
        rows = await _fetch_rows(client)

    biens: list[dict] = []
    for row in rows:
        try:
            b = _parse_row(row)
        except Exception:
            continue
        if b:
            biens.append(b)
    print(f"[ImmoTransactions] {len(biens)} biens résidentiels avant résolution dept")

    # Résolution commune → (dept, cp) en lot (un seul appel réseau par nom).
    all_names = sorted({c for b in biens for c in b["_cands"]})
    mapping = await resolve_communes(all_names, departements)

    results: list[dict] = []
    seen_ids: set[str] = set()
    ecartes: list[dict] = []  # biens sans dept cible → diagnostic hors-zone

    for b in biens:
        dept = cp = ville = None
        for cand in b["_cands"]:
            d, c = mapping.get(_norm(cand), (None, None))
            if d in depts_set:
                dept, cp, ville = d, c, cand.strip()
                break

        if dept is None:
            ecartes.append(b)
            continue

        # garde-fou strict (redondant mais explicite) : 0 fuite
        b.pop("_cands", None)
        if cp and cp[:2] != dept:
            continue
        if dept not in depts_set:
            continue

        b["departement"] = dept
        b["code_postal"] = cp
        b["ville"] = ville[:80] if ville else None

        if prix_max and b["prix"] and b["prix"] > prix_max:
            continue
        if prix_min and b["prix"] and b["prix"] < prix_min:
            continue
        if surface_min and b["surface"] and b["surface"] < surface_min:
            continue

        aid = b["id_annonce"] or b["url"]
        if aid in seen_ids:
            continue
        seen_ids.add(aid)
        results.append(b)

    # Diagnostic : parmi les écartés, lesquels se résolvent en départements
    # HORS zone (preuve que Saulieu/21 & co sont bien exclus) vs non identifiés.
    hors_zone: list[str] = []
    non_resolus = 0
    if ecartes:
        diag_names = sorted({c for b in ecartes for c in b["_cands"]})
        # résolution diagnostique élargie (sans restriction de département cible).
        # Le cache du résolveur est indexé par NOM seul : il a déjà mémorisé ces
        # communes comme (None, None) faute de dept cible → on le purge pour
        # forcer une vraie résolution nationale (ex. SAULIEU → 21, EPOISSES → 21).
        from scrapers import _geo_resolver
        _geo_resolver._cache.clear()
        diag_map = await resolve_communes(diag_names, list(range(1, 96)))
        _geo_resolver._cache.clear()
        for b in ecartes:
            best = None
            for cand in b["_cands"]:
                d, _ = diag_map.get(_norm(cand), (None, None))
                if d:
                    best = (cand.strip(), d)
                    break
            if best:
                hors_zone.append(f"{best[0]}→{best[1]} « {b['titre'][:35]} »")
            else:
                non_resolus += 1

    print(
        f"[ImmoTransactions] {len(results)} retenus | "
        f"{len(hors_zone)} écartés hors-zone (ex Saulieu/21) | "
        f"{non_resolus} sans commune identifiée"
    )
    if hors_zone:
        print(f"[ImmoTransactions] hors-zone écartés : {hors_zone[:12]}")
    return results


if __name__ == "__main__":
    standalone_main(search, "Immo Transactions")

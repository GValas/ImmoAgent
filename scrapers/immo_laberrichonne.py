"""scrapers/immo_laberrichonne.py — Agence Immobilière La Berrichonne (Châteauroux, Indre 36)

Méthode : scrape_simple (httpx) — SSR HTML (site solocal/CMS à layout en tables)
URL pattern : /catalogue/{ID}/{Categorie}/0/reference
  - 9  : Maisons « à vendre à Châteauroux »
  - 22 : Maisons « à vendre à l'extérieur » (= périphérie / autres communes de l'Indre)
  - 11 / 24 : « Divers » (terrains, granges, propriétés...) — scannés aussi,
              filtrés ensuite sur le type de bien.
Le site est une agence LOCALE mono-département : tout son inventaire est dans
l'Indre (36). Aucun code postal n'apparaît dans les cartes ; SEUL le nom de
commune est exposé (« CHATEAUROUX - CENTRE VILLE », « DEOLS », « BUZANCAIS »...).

Stratégie filtre département (STRICT, 0 fuite) :
  → On résout le nom de commune (en-tête de carte) contre un dictionnaire
    embarqué des 241 communes de l'Indre (geo.api.gouv.fr, normalisé sans
    accents/espaces). Si la commune matche → on garde et on en déduit le code
    postal 36xxx. Si elle NE matche AUCUNE commune de l'Indre → on JETTE la carte
    (sécurité absolue contre une éventuelle annonce hors-36). 36 est l'unique
    département cible couvert par cette agence ; les autres sont ignorés.

Cartes : chaque produit = une <tr> contenant EXACTEMENT une mention « Réf. NNNN »
         et au moins une image /images/catalogue/{id}_*.jpg
  - Réf       : « Réf. 7232 »  → id_annonce
  - En-tête   : texte avant « Réf. » → commune (+ quartier)
  - Titre     : texte après la réf, en majuscules
  - Desc      : reste du bloc texte
  - Prix      : « 425 250 euros FAI »  (PAS de symbole €)
  - Surface   : « Env 190M² hab » / « 190 m² »  → première mention habitable
  - Photos    : /images/catalogue/{id}_*.jpg (préfixées par le domaine)
  - DPE       : « Diagnostic énergie {lettre} » quand présent
Pas de page détail séparée (le lien mène à /contact.html?ref=...). Toute
l'information utile est dans la carte de la liste.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re
import unicodedata

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.immo-laberrichonne.com"
PHOTOS_PER_CARD = 12

# Catégories de catalogue à scanner (maisons + divers, intra & extra Châteauroux)
CATALOGUE_PATHS = [
    "/catalogue/9/Maisons/0/reference",
    "/catalogue/22/Maisons/0/reference",
    "/catalogue/11/Divers/0/reference",
    "/catalogue/24/Divers/0/reference",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Types à exclure (le catalogue « Divers » peut contenir terrains/locaux)
_EXCLUDE_TYPE = re.compile(
    r"terrain|local|commerce|garage|parking|immeuble|bureau|fonds|appartement|"
    r"box|emplacement|hangar\b",
    re.IGNORECASE,
)

# Dictionnaire des communes de l'Indre (36) : nom normalisé → code postal.
# Source : geo.api.gouv.fr/departements/36/communes (normalisé sans accents/espaces).
# Sert de filtre département STRICT : une commune absente d'ici est rejetée.
INDRE_COMMUNES = {
    "aigurande": "36140", "aize": "36150", "ambrault": "36120", "anjouin": "36210",
    "ardentes": "36120", "argentonsurcreuse": "36200", "argy": "36500", "arpheuilles": "36700",
    "arthon": "36330", "azayleferron": "36290", "badeconlepin": "36200", "bagneux": "36210",
    "baraize": "36270", "baudres": "36110", "bazaiges": "36270", "beaulieu": "36310",
    "belabre": "36370", "bommiers": "36120", "bonneuil": "36310", "bouesse": "36200",
    "bougeslechateau": "36110", "bretagne": "36110", "briantes": "36400", "brion": "36110",
    "brives": "36100", "buxeuil": "36150", "buxieresdaillac": "36230", "buzancais": "36500",
    "ceaulmont": "36200", "celon": "36200", "chabris": "36210", "chaillac": "36310",
    "chalais": "36370", "champillet": "36160", "chasseneuil": "36800",
    "chassignolles": "36400", "chateauroux": "36000", "chatillonsurindre": "36700",
    "chavin": "36200", "chazelet": "36170", "chezelles": "36500", "chitray": "36800",
    "chouday": "36100", "ciron": "36300", "cleredubois": "36700", "clion": "36700",
    "cluis": "36340", "coings": "36130", "concremiers": "36300", "conde": "36100",
    "crevant": "36140", "crozonsurvauvre": "36140", "cuzion": "36190", "deols": "36130",
    "diors": "36130", "diou": "36260", "douadic": "36300", "dunet": "36310",
    "dunlepoelier": "36210", "ecueille": "36240", "eguzonchantome": "36270",
    "etrechet": "36120", "feusines": "36160", "flerelariviere": "36700", "fontenay": "36150",
    "fontgombault": "36220", "fontguenand": "36600", "fougerolles": "36230",
    "francillon": "36110", "fredille": "36180", "gargilessedampierre": "36190",
    "gehee": "36240", "giroux": "36150", "gournay": "36230", "guilly": "36150",
    "heugnes": "36180", "ingrandes": "36300", "issoudun": "36100", "jeulesbois": "36120",
    "jeumaloches": "36240", "laberthenoux": "36400", "labuxerette": "36140",
    "lachampenoise": "36100", "lachapelleorthemale": "36500",
    "lachapellesaintlaurian": "36150", "lachatre": "36400", "lachatrelanglin": "36170",
    "lacs": "36400", "lamottefeuilly": "36160", "lange": "36600", "laperouille": "36350",
    "lavernelle": "36600", "leblanc": "36300", "lemagny": "36400", "lemenoux": "36200",
    "lepechereau": "36200", "lepoinconnet": "36330", "lepontchretienchabenet": "36800",
    "lesbordes": "36100", "letranger": "36700", "levroux": "36110", "lignac": "36370",
    "lignerolles": "36160", "linge": "36220", "liniez": "36150", "lizeray": "36100",
    "lourdoueixsaintmichel": "36140", "lourouersaintlaurent": "36400", "luant": "36350",
    "lucaylelibre": "36150", "lucaylemale": "36360", "lurais": "36220", "lureuil": "36220",
    "luzeret": "36800", "lye": "36600", "lyssaintgeorges": "36230", "maillet": "36340",
    "malicornay": "36340", "maron": "36120", "martizay": "36220", "mauvieres": "36370",
    "menetousurnahon": "36210", "menetreolssousvatan": "36150", "meobecq": "36500",
    "merigny": "36220", "merssurindre": "36230", "meunetplanches": "36100",
    "meunetsurvatan": "36150", "mezieresenbrenne": "36290", "migne": "36800", "migny": "36260",
    "montchevrier": "36140", "montgivray": "36400", "montierchaume": "36130",
    "montipouret": "36230", "montlevicq": "36400", "mosnay": "36200", "mouhers": "36340",
    "mouhet": "36170", "moulinssurcephons": "36110", "murs": "36700",
    "neonssurcreuse": "36220", "neret": "36400", "neuillaylesbois": "36500",
    "neuvypailloux": "36100", "neuvysaintsepulchre": "36230", "niherne": "36250",
    "nohantvic": "36400", "nuretleferron": "36800", "obterre": "36290", "orsennes": "36190",
    "orville": "36210", "oulches": "36800", "palluausurindre": "36500", "parnac": "36170",
    "paudy": "36260", "paulnay": "36290", "pellevoisin": "36180", "perassay": "36160",
    "pommiers": "36190", "poulaines": "36210", "poulignynotredame": "36160",
    "poulignysaintmartin": "36160", "poulignysaintpierre": "36300", "preaux": "36240",
    "preuillylaville": "36220", "prissac": "36370", "pruniers": "36120", "reboursin": "36150",
    "reuilly": "36260", "rivarennes": "36800", "rosnay": "36300", "roussines": "36170",
    "rouvreslesbois": "36110", "ruffec": "36300", "saciergessaintmartin": "36170",
    "saintaigny": "36300", "saintaoustrille": "36100", "saintaout": "36120",
    "saintaubin": "36100", "saintbenoitdusault": "36170", "saintchartier": "36400",
    "saintchristopheenbazelle": "36210", "saintchristopheenboucherie": "36400",
    "saintcivran": "36170", "saintcyrandujambot": "36700", "saintdenisdejouhet": "36230",
    "saintefauste": "36100", "saintegemme": "36500", "saintelizaigne": "36260",
    "sainteseveresurindre": "36160", "saintflorentin": "36150", "saintgaultier": "36800",
    "saintgenou": "36500", "saintgeorgessurarnon": "36100", "saintgilles": "36170",
    "sainthilairesurbenaize": "36370", "saintlactencin": "36500", "saintmarcel": "36200",
    "saintmaur": "36250", "saintmedard": "36700", "saintmichelenbrenne": "36290",
    "saintpierredejards": "36260", "saintplantaire": "36190", "saintvalentin": "36100",
    "sarzay": "36230", "sassiergessaintgermain": "36120", "saulnay": "36290",
    "sauzelles": "36220", "sazeray": "36160", "segry": "36100", "sellessurnahon": "36180",
    "semblecay": "36210", "souge": "36500", "tendu": "36200", "thenay": "36800",
    "thevetsaintjulien": "36400", "thizay": "36100", "tilly": "36310",
    "tournonsaintmartin": "36220", "tranzault": "36230", "urciers": "36160",
    "valencay": "36600", "valfouzon": "36210", "vatan": "36150", "velles": "36330",
    "venduvres": "36500", "verneuilsurigneraie": "36400", "veuil": "36600",
    "vicqexemplet": "36400", "vicqsurnahon": "36600", "vigoulant": "36160", "vigoux": "36170",
    "vijon": "36160", "villedieusurindre": "36320", "villegongis": "36110",
    "villegouin": "36500", "villentroisfaverollesenberry": "36360", "villiers": "36290",
    "vineuil": "36110", "vouillon": "36100",
}

# Communes les plus longues d'abord → priorité au match le plus spécifique
_COMMUNE_KEYS = sorted(INDRE_COMMUNES.keys(), key=len, reverse=True)

_REF_RE = re.compile(r"R[ée]f\.\s*(\d+)")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    # Agence mono-département : pertinente uniquement si 36 est demandé
    if "36" not in departements:
        print("[Berrichonne] Dept 36 non demandé → 0 annonce")
        return []

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        for path in CATALOGUE_PATHS:
            try:
                cards = await _scrape_catalogue(client, path)
            except Exception as e:
                print(f"[Berrichonne] Erreur {path}: {e}")
                await asyncio.sleep(0.5)
                continue

            kept = 0
            for bien in cards:
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
                kept += 1
            print(f"[Berrichonne] {path}: {kept} annonces retenues (36)")
            await asyncio.sleep(0.5)

    return results


async def _scrape_catalogue(client: httpx.AsyncClient, path: str) -> list[dict]:
    r = await client.get(BASE_URL + path)
    if r.status_code != 200:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    biens: list[dict] = []

    for tr in soup.find_all("tr"):
        refs = tr.find_all(string=_REF_RE)
        if len(refs) != 1:
            continue
        imgs = [
            i.get("src")
            for i in tr.select("img")
            if "catalogue" in (i.get("src") or "")
        ]
        if not imgs:
            continue
        try:
            bien = _parse_card(tr)
        except Exception:
            continue
        if bien:
            biens.append(bien)

    return biens


def _parse_card(tr) -> dict | None:
    text = tr.get_text("\n", strip=True)
    # Normalise les espaces insécables → espaces ordinaires (prix « 425 250 »)
    text = text.replace(" ", " ").replace(" ", " ")

    m_ref = _REF_RE.search(text)
    if not m_ref:
        return None
    ref = m_ref.group(1)

    # En-tête = texte avant « Réf. » (commune + quartier), nettoyé du chrome
    head = _REF_RE.split(text)[0]
    for junk in ("Toutes les photos", "CONTACTEZ-NOUS", "A vendre"):
        head = head.replace(junk, " ")
    head = head.strip(" \n\t-›")
    # Ignorer les en-têtes de navigation résiduels
    if "Produit" in head or "rubrique" in head or "Divers" in head and "›" in head:
        return None

    ville, code_postal = _resolve_commune(head, text)
    # Filtre département STRICT : pas de commune Indre reconnue → on jette
    if not code_postal:
        return None

    # Statut « VENDU » → on ignore
    if "VENDU" in head.upper():
        return None

    # Titre = portion après la réf, jusqu'au prix (souvent en majuscules)
    after = _REF_RE.split(text, maxsplit=1)
    tail = after[-1] if len(after) > 1 else ""
    lines = [ln.strip(" -\n") for ln in tail.split("\n") if ln.strip(" -\n")]
    titre = lines[0] if lines else f"Maison {ville}"

    # Type de bien : déduit du TITRE seulement (le titre annonce le type ;
    # la description mentionne souvent « jardin », « terrain », « grange » en
    # annexe et ne doit pas requalifier une maison). On évite aussi l'en-tête
    # (commune « CHATEAUROUX » → faux « chateau »).
    type_bien = _guess_type(titre.lower())
    if _EXCLUDE_TYPE.search(type_bien):
        return None

    # Prix : « 425 250 euros FAI »
    prix = _parse_price(text)

    # Surface habitable
    surface = _parse_surface(text)

    # DPE
    dpe = _parse_dpe(text)

    # Photos
    photos = []
    for i in tr.select("img"):
        src = i.get("src") or ""
        if "catalogue" in src and not src.startswith("data:"):
            if src.startswith("/"):
                src = BASE_URL + src
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    description = " ".join(lines)[:1200]

    return {
        "source": "immo_laberrichonne",
        "url": f"{BASE_URL}/contact.html?ref=Réf. {ref}",
        "id_annonce": ref,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description,
        "departement": "36",
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": _parse_terrain(text),
        "pieces": _parse_pieces(text),
        "chambres": _parse_chambres(text),
        "prix": prix,
        "photos": photos,
        "dpe": dpe,
        "agence": "Agence Immobilière La Berrichonne",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_commune(head: str, full_text: str) -> tuple[str, str]:
    """Identifie une commune de l'Indre dans l'en-tête (puis le texte) de la carte.

    Renvoie (nom_affichable, code_postal) si une commune du 36 est reconnue,
    sinon ('', '') → la carte sera rejetée (filtre département strict).
    """
    nhead = _norm(head)
    # 1) Match exact d'un mot de l'en-tête sur une commune
    #    (priorité aux noms les plus longs pour éviter les sous-chaînes parasites)
    for key in _COMMUNE_KEYS:
        if key and key in nhead:
            return _display_commune(head, key), INDRE_COMMUNES[key]
    # 2) Repli : chercher une commune dans le texte complet de l'annonce
    nfull = _norm(full_text)
    for key in _COMMUNE_KEYS:
        # éviter les communes très courtes (faux positifs) au repli
        if len(key) >= 5 and key in nfull:
            return _display_commune(head, key), INDRE_COMMUNES[key]
    return "", ""


def _display_commune(head: str, key: str) -> str:
    """Nom de commune lisible : 1er segment de l'en-tête (avant un tiret), sinon clé."""
    seg = re.split(r"[-–]", head)[0].strip()
    seg = re.sub(r"\s+", " ", seg)
    return seg.title() if seg else key.title()


def _parse_price(text: str) -> float | None:
    m = re.search(r"([\d][\d\s ]{2,})\s*euros", text, re.IGNORECASE)
    if not m:
        m = re.search(r"([\d][\d\s ]{2,})\s*€", text)
    if not m:
        return None
    val = re.sub(r"[\s ]", "", m.group(1))
    try:
        f = float(val)
        return f if f >= 1000 else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """Surface HABITABLE uniquement, via mentions fiables :
    « NNN m² hab », « NNN m² habitable », ou « Env NNN M² hab ».
    On NE prend PAS la 1ʳᵉ valeur m² venue (souvent une pièce ou le terrain)."""
    for pat in (
        r"(\d[\d\s\u00a0]*)\s*[mM]²?\s*hab\b",
        r"(\d[\d\s\u00a0]*)\s*[mM]²?\s*habitable",
        r"[Ee]nv\.?\s*(\d[\d\s\u00a0]*)\s*[mM]²?\s*hab",
        r"surface\s+habitable[^\d]{0,12}(\d[\d\s\u00a0]*)\s*[mM]",
    ):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = re.sub(r"[\s\u00a0]", "", m.group(1))
            try:
                f = float(val)
                if 8 <= f <= 2000:
                    return f
            except ValueError:
                pass
    return None


def _parse_terrain(text: str) -> float | None:
    m = re.search(
        r"terrain[^\d]{0,20}?(\d[\d\s ]*)\s*m[²2]", text, re.IGNORECASE
    )
    if not m:
        m = re.search(r"(\d[\d\s ]{2,})\s*m[²2]\s*de\s+terrain", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s ]", "", m.group(1))
        try:
            f = float(val)
            if 50 <= f <= 500000:
                return f
        except ValueError:
            pass
    return None


def _parse_pieces(text: str) -> int | None:
    m = re.search(r"(\d+)\s*pi[èe]ces?", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_chambres(text: str) -> int | None:
    m = re.search(r"(\d+)\s*chambres?", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_dpe(text: str) -> str | None:
    m = re.search(
        r"(?:Diagnostic\s+[ée]nergie|DPE|classe\s+[ée]nerg\w*)\s*[:\-]?\s*([A-G])\b",
        text, re.IGNORECASE,
    )
    if m:
        return m.group(1).upper()
    return None


def _guess_type(blob: str) -> str:
    # (motif regex avec \b, étiquette) — l'ordre fixe la priorité
    for pat, label in (
        (r"long[èe]re", "longère"),
        (r"fermette", "fermette"),
        (r"\bferme\b", "ferme"),
        (r"\bmanoir\b", "manoir"),
        (r"ch[âa]teau\b", "château"),
        (r"propri[ée]t[ée]", "propriété"),
        (r"\bdemeure\b", "demeure"),
        (r"\bmoulin\b", "moulin"),
        (r"\bdomaine\b", "domaine"),
        (r"maison\s+bourgeoise", "maison bourgeoise"),
        (r"\bvilla\b", "villa"),
        (r"\bterrain\b", "terrain"),
        (r"appartement", "appartement"),
        (r"local\b", "local"),
        (r"\bimmeuble\b", "immeuble"),
        (r"\bgrange\b", "grange"),
        (r"\bpavillon\b", "maison"),
        (r"\bmaison\b", "maison"),
    ):
        if re.search(pat, blob, re.IGNORECASE):
            return label
    return "maison"


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
    print(f"\nTotal La Berrichonne: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:15]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:50]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

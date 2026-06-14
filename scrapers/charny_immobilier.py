"""scrapers/charny_immobilier.py — Agence Charny Immobilier (Charny/Puisaye, 89/45)

Méthode : api_inoff (httpx) — moteur **Win Immobilier (Consulog)**.
Le site est en routage par hash (#!/annonces-immobilieres-...) : la page d'accueil
ne rend en SSR que ~10 « biens vedettes ». MAIS le front charge la liste COMPLÈTE
via un POST AJAX qui renvoie tout le HTML des annonces en une requête :

    POST https://charny-immobilier.com/ajax.listbien.php
    data : ville=Toutes les villes
    → ~1145 <article class="annonces" ...> avec attributs data-* exploitables :
        data-ref      référence interne
        data-prix     "p229000" → 229000 €
        data-piece    "p7"      → 7 pièces
        data-surfhab  "sh150"   → 150 m² habitables
        data-chambre  "c3"      → 3 chambres
      et itemprop name = "{Type} - {VILLE} (CP)" → type, ville, code postal.

Filtre département (0 fuite) : CP extrait du libellé (… (89120)), POST-FILTRE STRICT
CP[:2] ∈ criteres['departements']. Non-résidentiel exclu sur le type.

Volume zone : ~545 biens 89 (Yonne) + ~42 biens 45 (Loiret) — profil rural
(fermettes, granges, corps de ferme, pavillons, propriétés).

Détail : la fiche est CSR (hash), description complète non récupérable en httpx ;
on s'appuie sur les data-* de la liste, qui suffisent au filtrage structurel.
photos : la vignette `data-img` est relative au domaine.

Interface : async def search(criteres: dict) -> list[dict]
"""
import re

from bs4 import BeautifulSoup

from scrapers._base import make_client

BASE_URL = "https://charny-immobilier.com"
LIST_ENDPOINT = f"{BASE_URL}/ajax.listbien.php"

_EXCLUDE_TYPE = re.compile(
    r"(appartement|immeuble|terrain|garage|local|parking|bureau|commerce|"
    r"fonds|stationnement|viager|cave|box)",
    re.IGNORECASE,
)


def _data_num(card, attr: str):
    """Extrait l'entier d'un attribut data du type 'p229000' / 'sh150' / 'c3'."""
    val = card.get(attr) or ""
    m = re.search(r"(\d+)", val)
    return int(m.group(1)) if m else None


def _parse_card(card) -> dict | None:
    ref = card.get("data-ref")
    if not ref:
        return None

    # libellé "name" : "Grange - CHARNY OREE DE PUISAYE (89120)"
    name_el = card.find(attrs={"itemprop": "name"})
    libelle = name_el.get_text(" ", strip=True) if name_el else ""
    cp_m = re.search(r"\((\d{5})\)", libelle)
    if not cp_m:
        return None
    code_postal = cp_m.group(1)

    type_bien = (libelle.split("-", 1)[0].strip() or "").lower()
    if not type_bien or _EXCLUDE_TYPE.search(type_bien):
        return None

    # ville = entre le "- " et le " (CP"
    ville = ""
    vm = re.search(r"-\s*(.+?)\s*\(\d{5}\)", libelle)
    if vm:
        ville = vm.group(1).strip().title()

    href_el = card.find("a", class_="block")
    href = href_el.get("href") if href_el else ""
    url = f"{BASE_URL}/{href.lstrip('/')}" if href else BASE_URL

    prix = _data_num(card, "data-prix")
    pieces = _data_num(card, "data-piece")
    surface = _data_num(card, "data-surfhab")
    chambres = _data_num(card, "data-chambre")

    photos: list[str] = []
    img = card.find("img", attrs={"data-img": True})
    if img and img.get("data-img"):
        src = img["data-img"]
        if not src.startswith("http"):
            src = f"{BASE_URL}/{src.lstrip('/')}"
        photos = [src]

    titre = f"{type_bien.title()} {pieces or ''} pièces {ville} ({code_postal})".replace(
        "  ", " "
    ).strip()

    return {
        "source": "charny_immobilier",
        "url": url,
        "id_annonce": ref,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": libelle[:1200],
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix if prix else None,
        "photos": photos,
        "dpe": None,
        "agence": "Charny Immobilier",
    }


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    if not departements:
        return []

    prix_min = criteres.get("prix_min")
    prix_max = criteres.get("prix_max")
    surface_min = criteres.get("surface_min")

    results: list[dict] = []
    seen: set[str] = set()

    async with make_client() as client:
        try:
            r = await client.post(
                LIST_ENDPOINT,
                data={"ville": "Toutes les villes"},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        except Exception as e:
            print(f"[Charny] POST ajax.listbien.php échec : {e}")
            return []
        if r.status_code != 200:
            print(f"[Charny] ajax.listbien.php status={r.status_code}")
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("article.annonces")
        print(f"[Charny] {len(cards)} cartes brutes (ajax.listbien.php)")

        for card in cards:
            bien = _parse_card(card)
            if not bien:
                continue
            if bien["departement"] not in departements:   # POST-FILTRE DEPT STRICT
                continue
            if bien["id_annonce"] in seen:
                continue
            # bornes prix/surface quand le champ est connu
            if prix_max and bien["prix"] and bien["prix"] > prix_max:
                continue
            if prix_min and bien["prix"] and bien["prix"] < prix_min:
                continue
            if surface_min and bien["surface"] and bien["surface"] < surface_min:
                continue
            seen.add(bien["id_annonce"])
            results.append(bien)

    print(f"[Charny] Total: {len(results)} biens")
    return results


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Charny Immobilier")

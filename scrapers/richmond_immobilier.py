"""scrapers/richmond_immobilier.py — Groupe Richmond Immobilier (Nevers / Fourchambault)

Méthode : scrape_simple (httpx) — SSR HTML
Site : https://www.richmondimmobilier-nevers-fourchambault.fr (petite agence familiale)
URL liste : /catalog/annonce.php  (page unique, ~11 cartes, PAS de pagination)
Cartes : div.item-product
  .products-name  → titre (ex "EN VENTE NEVERS - MAISON INDEPENDANTE 5 PIECES")
  .products-desc  → description
  .products-ref   → "Ref. : F5474"
  .products-price → "99 500 €"
  a[href*="/fiches/"] → lien détail relatif "../fiches/..._NNNNNNNN/...html"
  img.photo src "../office20/..." (vignette)

Filtre département (0 fuite) :
  Le code postal n'est PAS dans la carte. La ville est dans le titre.
  Le formulaire de recherche contient un <select> (name="C_65_tmp") dont chaque
  <option value="CP NOM-VILLE"> mappe une ville à son code postal. On parse ces
  options → dict { ville_normalisée : (code_postal, dept) }. Pour chaque carte on
  extrait le nom de ville du titre, on le normalise et on le cherche dans ce dict.
  Si introuvable → repli regex CP 5 chiffres dans la description ; sinon le bien
  est ignoré (prudence > fuite). Post-filtre STRICT : dept ∈ criteres['departements'].

Interface : async def search(criteres: dict) -> list[dict]
"""
from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup

from scrapers._base import get_with_retry, make_client, parse_int, parse_price

BASE_URL = "https://www.richmondimmobilier-nevers-fourchambault.fr"
LIST_URL = f"{BASE_URL}/catalog/annonce.php"

# Types non-résidentiels à exclure (détectés dans le titre).
_EXCLUDE_TYPE = re.compile(
    r"appartement|terrain\s+(?:de|à)|garage|locaux|local\b|commercial|commerce|"
    r"entrepot|entrepôt|immeuble|bureau|fonds|parking",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    """Normalise un nom de ville : minuscule, sans accents, '-' → espace, espaces compactés."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower().replace("-", " ")
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _resolve_abs(href: str) -> str:
    """Transforme un lien relatif "../fiches/..." en URL absolue."""
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return BASE_URL + "/" + href.lstrip("./")


def _build_ville_index(soup: BeautifulSoup) -> dict[str, tuple[str, str]]:
    """Construit { ville_normalisée : (code_postal, dept) } depuis les <option> du
    formulaire dont la value est "CP NOM-VILLE" (ex "58000 NEVERS")."""
    index: dict[str, tuple[str, str]] = {}
    for opt in soup.find_all("option"):
        value = (opt.get("value") or "").strip()
        m = re.match(r"^(\d{5})\s+(.+)$", value)
        if not m:
            continue
        cp, ville = m.group(1), m.group(2)
        index[_norm(ville)] = (cp, cp[:2])
    return index


def _ville_from_title(titre: str) -> str:
    """Extrait le nom de ville d'un titre type "EN VENTE NEVERS - MAISON ...".
    On retire le préfixe "EN VENTE"/"A VENDRE" puis on prend le segment avant " - "."""
    t = titre.strip()
    t = re.sub(r"^\s*(en\s+vente|a\s+vendre|vente)\b", "", t, flags=re.IGNORECASE).strip()
    # Le nom de ville est généralement le segment avant le premier tiret séparateur.
    segment = re.split(r"\s[-–]\s", t, maxsplit=1)[0]
    return segment.strip()


def _match_ville(titre: str, description: str,
                 index: dict[str, tuple[str, str]]) -> tuple[str, str | None, str | None]:
    """Retourne (ville, code_postal, dept). cp/dept à None si non résolu."""
    ville_brute = _ville_from_title(titre)
    norm = _norm(ville_brute)

    # 1) Match exact dans l'index du formulaire.
    if norm in index:
        cp, dept = index[norm]
        return ville_brute, cp, dept

    # 2) Match partiel : une ville de l'index est contenue dans le titre normalisé.
    norm_titre = _norm(titre)
    for ville_norm, (cp, dept) in index.items():
        if ville_norm and re.search(rf"\b{re.escape(ville_norm)}\b", norm_titre):
            return ville_brute, cp, dept

    # 3) Repli : un CP à 5 chiffres dans la description.
    m = re.search(r"\b(\d{5})\b", description or "")
    if m:
        cp = m.group(1)
        return ville_brute, cp, cp[:2]

    return ville_brute, None, None


def _parse_surface(text: str) -> float | None:
    m = re.search(r"(\d[\d\s\xa0]*)\s*m[²2]", text or "", re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if 8 <= f <= 2000:
                return f
        except ValueError:
            pass
    return None


def _parse_card(card, index: dict[str, tuple[str, str]]) -> dict | None:
    name_el = card.select_one(".products-name")
    titre = name_el.get_text(" ", strip=True) if name_el else ""
    titre = re.sub(r"\s+", " ", titre).strip()
    if not titre:
        return None

    # Exclure les types non-résidentiels.
    if _EXCLUDE_TYPE.search(titre):
        return None

    desc_el = card.select_one(".products-desc")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""
    description = re.sub(r"\s+", " ", description).strip()

    ville, code_postal, dept = _match_ville(titre, description, index)
    if not code_postal or not dept:
        return None  # ville non résolue → on ignore (prudence > fuite)

    link = card.select_one('a[href*="/fiches/"]')
    href = link.get("href", "") if link else ""
    url = _resolve_abs(href)
    if not url:
        return None

    ref_el = card.select_one(".products-ref")
    ref = ""
    if ref_el:
        rm = re.search(r"Ref\.?\s*:\s*([A-Za-z0-9\-]+)", ref_el.get_text(" ", strip=True))
        ref = rm.group(1) if rm else ""
    id_num = ""
    im = re.search(r"_(\d+)/", href)
    if im:
        id_num = im.group(1)
    id_annonce = ref or id_num or url

    price_el = card.select_one(".products-price")
    prix = parse_price(price_el.get_text(" ", strip=True) if price_el else "")
    if prix is None:
        # Repli : "PRIX 131000" parfois présent dans la description.
        pm = re.search(r"PRIX\s+([\d\s\xa0]+)", description, re.IGNORECASE)
        if pm:
            prix = parse_price(pm.group(1))

    surface = _parse_surface(description) or _parse_surface(titre)
    pieces = parse_int(r"(\d+)\s*PIECES?", titre)

    # Type de bien : maison par défaut (longère/villa/propriété conservées telles quelles).
    type_bien = "maison"
    tm = re.search(r"\b(maison|longere|longère|villa|propriete|propriété|ferme|"
                   r"pavillon|demeure|chateau|château)\b", titre, re.IGNORECASE)
    if tm:
        type_bien = tm.group(1).lower()

    photos: list[str] = []
    img = card.select_one("img.photo") or card.select_one("img")
    if img:
        src = img.get("data-src") or img.get("src") or ""
        if src and not src.startswith("data:"):
            abs_src = _resolve_abs(src)
            if abs_src:
                photos.append(abs_src)

    return {
        "source": "richmond_immobilier",
        "url": url,
        "id_annonce": id_annonce,
        "titre": titre[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": dept,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": None,
        "pieces": pieces,
        "chambres": None,
        "prix": prix,
        "photos": photos,
        "dpe": None,
        "agence": "Richmond Immobilier",
    }


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen_ids: set[str] = set()
    depts_vus_avant_prix: set[str] = set()

    async with make_client() as client:
        r = await get_with_retry(client, LIST_URL)
        if r is None or r.status_code != 200:
            print(f"[Richmond] Liste inaccessible (status={getattr(r, 'status_code', 'None')})")
            return results

        soup = BeautifulSoup(r.text, "html.parser")
        index = _build_ville_index(soup)
        print(f"[Richmond] {len(index)} villes mappées depuis le formulaire")

        cards = soup.select("div.item-product")
        for card in cards:
            try:
                bien = _parse_card(card, index)
            except Exception:
                continue
            if not bien:
                continue

            dept = bien["departement"]
            cp = str(bien.get("code_postal") or "")
            # Post-filtre département STRICT (0 fuite) : CP cohérent + dept cible.
            if cp and cp[:2] != dept:
                continue
            depts_vus_avant_prix.add(dept)
            if dept not in departements:
                continue

            aid = bien.get("id_annonce") or bien.get("url")
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

    print(f"[Richmond] Départements vus (avant filtre prix/zone) : "
          f"{sorted(depts_vus_avant_prix)}")
    print(f"[Richmond] {len(results)} annonces retenues (zone + prix)")
    return results


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Richmond Immobilier")

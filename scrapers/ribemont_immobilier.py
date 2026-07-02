"""scrapers/ribemont_immobilier.py — Ribemont Immobilier (agence locale Aisne 02)

Méthode : scrape_simple (httpx) — SSR HTML (site PHP statique, nginx, pas de JS).

Agence mono-secteur basée à Ribemont (02240), Picardie / Vallée de l'Oise,
rayon ~30 min autour de Saint-Quentin. Tout l'inventaire est dans l'Aisne (02)
→ pas de filtre département côté serveur ; on POST-FILTRE strictement sur
`code_postal[:2] == dept`. Pour les départements hors-02 (ex. 72/28/45/89), le
scraper renvoie donc légitimement 0 bien (aucune fuite possible).

URL liste (pas de filtre dept ni pagination — petit stock) :
  /vente-maisons_cl1.html?categorie1=3
  /vente-appartements_cl1.html?categorie1=4
  /vente-terrains_cl1.html?categorie1=6      (ignoré : terrains)
  /vente-commerces_cl1.html?categorie1=5     (ignoré : commerces)
  (Note : /nos-biens.php redirige vers 404.php — chemin obsolète, ne pas utiliser.)

Cartes liste : li.list_prod
  - URL    : a.encart-produit[href]  → "{slug}_cd1_{id}.html"
  - Titre  : .title_listing
  - Réf    : .ref-produit
  - Surface: .surface-produit  → "113m²"
  - Prix   : .prix-produit     → "100 500,00 €"
  - Photo  : .visuelle img[src]
La liste ne contient PAS le code postal → on ouvre chaque page détail.

Page détail (clé géo : code postal) :
  - h1 / .titre-detail  → "... CP : 02440"
  - .detail-info-bien > div  → paires "Libellé : valeur" :
      Type de bien, Prix de vente, Superficie totale, Superficie jardin (terrain),
      Nombre de pièces, Nombre de chambres ...
  - Commentaires : bloc texte descriptif
  - Photos pleine taille : a.lightbox[href] /photos/*.jpg
  - DPE : souvent "en attente" (dpe_attente_2024.png) → dpe = None.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.ribemont-immobilier.com"
PHOTOS_PER_CARD = 12

# Catégories de liste à parcourir (on ne garde que maisons + appartements).
CATEGORY_URLS = [
    "/vente-maisons_cl1.html?categorie1=3",
    "/vente-appartements_cl1.html?categorie1=4",
]


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:
        # 1) Collecte des URLs détail sur toutes les pages catégorie
        detail_hrefs: list[str] = []
        seen_href: set[str] = set()
        for cat_url in CATEGORY_URLS:
            try:
                r = await client.get(BASE_URL + cat_url)
                if r.status_code != 200:
                    continue
                cards = BeautifulSoup(r.text, "html.parser").select("li.list_prod")
                for card in cards:
                    link = card.select_one("a.encart-produit") or card.select_one("a[href]")
                    href = link.get("href", "") if link else ""
                    if not href or "_cd1_" not in href:
                        continue
                    full = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"
                    if full not in seen_href:
                        seen_href.add(full)
                        detail_hrefs.append(full)
            except Exception as e:
                print(f"[Ribemont] Erreur liste {cat_url}: {e}")
            await asyncio.sleep(0.5)

        print(f"[Ribemont] {len(detail_hrefs)} annonce(s) listée(s)")

        # 2) Page détail (porte le code postal → filtre département)
        for url in detail_hrefs:
            try:
                bien = await _scrape_detail(client, url)
            except Exception as e:
                print(f"[Ribemont] Erreur détail {url}: {e}")
                bien = None
            if not bien:
                await asyncio.sleep(0.5)
                continue

            cp = bien.get("code_postal") or ""
            # POST-FILTRE STRICT : on n'accepte que les départements cibles.
            if not cp or cp[:2] not in departements:
                await asyncio.sleep(0.5)
                continue
            bien["departement"] = cp[:2]

            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                await asyncio.sleep(0.5)
                continue
            if prix_min and p and p < prix_min:
                await asyncio.sleep(0.5)
                continue
            if surface_min and s and s < surface_min:
                await asyncio.sleep(0.5)
                continue

            results.append(bien)
            await asyncio.sleep(0.5)

    print(f"[Ribemont] {len(results)} annonce(s) retenue(s)")
    return results


async def _scrape_detail(client: httpx.AsyncClient, url: str) -> dict | None:
    r = await client.get(url)
    if r.status_code != 200 or "404.php" in str(r.url):
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    # Titre + code postal (h1 : "Maison vente {titre} {Type} {CP}")
    h1 = soup.select_one(".encart_detail h1") or soup.select_one("h1")
    h1_text = h1.get_text(" ", strip=True) if h1 else ""

    titre_el = soup.select_one(".titre-detail")
    titre_text = titre_el.get_text(" ", strip=True) if titre_el else h1_text

    code_postal = ""
    m_cp = re.search(r"CP\s*:?\s*(\d{5})", titre_text) or re.search(r"\b(\d{5})\b", h1_text)
    if m_cp:
        code_postal = m_cp.group(1)

    # Paires "Libellé : valeur" de la fiche
    info = soup.select_one(".detail-info-bien")
    pairs: dict[str, str] = {}
    if info:
        for d in info.find_all("div"):
            txt = d.get_text(" ", strip=True)
            if " : " in txt:
                k, _, v = txt.partition(" : ")
                k = k.strip().lower()
                if k and k not in pairs:
                    pairs[k] = v.strip()

    type_bien = (pairs.get("type de bien") or "").strip() or "Maison"
    # On ne garde que maisons / appartements.
    tl = type_bien.lower()
    if not ("maison" in tl or "appartement" in tl or "villa" in tl or "longère" in tl
            or "longere" in tl or "propriété" in tl or "propriete" in tl):
        return None

    prix = _to_float(pairs.get("prix de vente") or pairs.get("prix de vente honoraires inclus"))
    surface = _to_float(pairs.get("superficie totale") or pairs.get("superficie habitable"))
    surface_terrain = _to_float(pairs.get("superficie jardin") or pairs.get("superficie terrain"))
    pieces = _to_int(pairs.get("nombre de pièces") or pairs.get("nombre de pieces"))
    chambres = _to_int(pairs.get("nombre de chambres"))

    # Référence
    ref = ""
    ref_el = soup.select_one(".reference")
    if ref_el:
        ref = re.sub(r"R[ée]f[ée]rence\s*:?\s*", "", ref_el.get_text(" ", strip=True), flags=re.I).strip()
    if not ref:
        m_id = re.search(r"_cd1_(\d+)\.html", url)
        ref = m_id.group(1) if m_id else url

    # Ville (souvent absente : agence en "Secteur"). On déduit depuis le titre h1
    # si présent, sinon None — le CP suffit au filtre/pipeline.
    titre = h1_text or titre_text
    titre = re.sub(r"^\s*(Maison|Appartement)\s+vente\s+", "", titre, flags=re.I).strip()
    # retire le suffixe technique "{Type} {CP}" parfois collé en fin de h1
    titre = re.sub(r"\s+(Maison|Appartement|Villa)\s+\d{5}\s*$", "", titre, flags=re.I).strip()
    titre = re.sub(r"\s+\d{5}\s*$", "", titre).strip()
    ville = None

    # Description (commentaires)
    description = ""
    m_com = soup.find(string=re.compile(r"Commentaires", re.I))
    if m_com:
        parent = m_com.find_parent()
        if parent:
            description = parent.get_text(" ", strip=True)
            description = re.sub(r"^\s*Commentaires\s*:?\s*", "", description, flags=re.I).strip()

    # Photos pleine taille
    photos: list[str] = []
    for a in soup.select("a.lightbox[href], a.smallPicture[href]"):
        href = a.get("href", "")
        if href and "/photos/" in href and href.lower().endswith((".jpg", ".jpeg", ".png")):
            full = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"
            if full not in photos:
                photos.append(full)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "ribemont_immobilier",
        "url": url,
        "id_annonce": ref,
        "titre": (titre or type_bien)[:150],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": code_postal[:2] if code_postal else None,
        "ville": ville,
        "code_postal": code_postal or None,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": pieces,
        "chambres": chambres,
        "prix": prix,
        "photos": photos,
        "dpe": None,  # DPE souvent "en attente" sur le site
        "agence": "Ribemont Immobilier",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _to_float(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[€\s\xa0]", "", text).replace(",", ".")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    # garde le 1er nombre décimal
    m = re.match(r"\d+(?:\.\d+)?", cleaned)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _to_int(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


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
    print(f"\nTotal Ribemont Immobilier: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']}"
        )

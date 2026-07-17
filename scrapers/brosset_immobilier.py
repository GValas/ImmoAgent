"""scrapers/brosset_immobilier.py — Brosset Immobilier / Brosset Val de Loire
(Tours, Amboise, Loches — Indre-et-Loire, 37)

Méthode : scrape_simple (httpx) — Next.js SSR : l'état COMPLET de la liste est
embarqué dans le JSON `<script id="__NEXT_DATA__">` (props.pageProps.ssrDataBiens),
avec des biens ENTIÈREMENT structurés : affNum, typologie, prix (vente.prix),
surfaceHabitable, surfaceTerrain, nbrePieces, nbreChambres, dpeEtiquette,
codePostal, ville.libelle, commentaire, images[].contentUrl. Aucun parsing HTML.

URL pattern : /achat/maison?page={N} (pagination via pageProps.ssrLastPage ;
stock maisons observé : ~15 biens, une seule page). PAS de filtre département
côté serveur (agence mono-département 37) → post-filtre STRICT code_postal[:2].

Ne requête que si le 37 est demandé.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import json
import re

from scrapers._base import (
    _jitter,
    get_with_retry,
    make_client,
    standalone_main,
)

BASE_URL = "https://www.brosset-immobilier.fr"
SOURCE = "brosset_immobilier"
LABEL = "BrossetImmo"
AGENCE = "Brosset Immobilier"
DEPTS_AGENCE = {"37"}
MAX_PAGES = 10
PHOTOS_PER_BIEN = 10

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)

# Typologies house-like conservées (le flux /achat/maison ne renvoie en principe
# que des maisons, garde-fou si d'autres typologies s'y glissent).
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|gite|gîte|pavillon",
    re.IGNORECASE,
)


def _parse_bien(raw: dict) -> dict | None:
    typologie = str(raw.get("typologie") or "")
    if not _KEEP_TYPE.search(typologie):
        return None

    code_postal = str(raw.get("codePostal") or "")
    if not code_postal:
        return None

    ville_obj = raw.get("ville") or {}
    ville = str(ville_obj.get("libelle") or ville_obj.get("ville") or "")

    vente = raw.get("vente") or {}
    prix = vente.get("prix")

    bien_id = raw.get("id")
    aff_num = str(raw.get("affNum") or "")
    id_annonce = aff_num or (str(bien_id) if bien_id else "")

    # Description : commentaire HTML (<BR>) → texte plat
    description = re.sub(r"<[^>]+>", " ", str(raw.get("commentaire") or ""))
    description = re.sub(r"\s+", " ", description).strip()

    surface = raw.get("surfaceHabitable") or raw.get("surfaceBien")
    surface_terrain = raw.get("surfaceTerrain")

    photos = []
    for img in (raw.get("images") or [])[:PHOTOS_PER_BIEN]:
        cu = str(img.get("contentUrl") or "")
        if cu:
            photos.append(cu if cu.startswith("http") else BASE_URL + cu)

    dpe = str(raw.get("dpeEtiquette") or "").upper() or None
    if dpe and dpe not in "ABCDEFG":
        dpe = None

    titre = f"{typologie.title()} {raw.get('nbrePieces') or '?'} pièces {ville}".strip()
    # URL détail : /annonce/achat/{typologie}/{ville-slug}/{affNum}
    ville_slug = str(ville_obj.get("slug") or "").strip("/")
    if aff_num and ville_slug:
        url = f"{BASE_URL}/annonce/achat/{typologie.lower()}/{ville_slug}/{aff_num}"
    else:
        url = f"{BASE_URL}/achat/maison"

    return {
        "source": SOURCE,
        "url": url,
        "id_annonce": id_annonce or url,
        "titre": titre[:150],
        "type_bien": typologie or "maison",
        "description": description[:1200],
        "departement": code_postal[:2],
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": float(surface) if surface else None,
        "surface_terrain": float(surface_terrain) if surface_terrain else None,
        "pieces": raw.get("nbrePieces"),
        "chambres": raw.get("nbreChambres"),
        "prix": float(prix) if prix else None,
        "photos": photos,
        "dpe": dpe,
        "agence": AGENCE,
    }


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    cibles = departements & DEPTS_AGENCE
    if not cibles:
        return []

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    biens: list[dict] = []
    seen_ids: set[str] = set()

    async with make_client() as client:
        page, last_page = 1, 1
        while page <= min(last_page, MAX_PAGES):
            r = await get_with_retry(client, f"{BASE_URL}/achat/maison?page={page}")
            if r is None or r.status_code != 200:
                break
            m = _NEXT_DATA_RE.search(r.text)
            if not m:
                break
            try:
                pp = json.loads(m.group(1))["props"]["pageProps"]
            except Exception:
                break
            last_page = int(pp.get("ssrLastPage") or 1)
            raws = pp.get("ssrDataBiens") or []
            if not raws:
                break

            for raw in raws:
                try:
                    bien = _parse_bien(raw)
                except Exception:
                    continue
                if not bien:
                    continue
                aid = bien["id_annonce"]
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                # Garde-fou département STRICT : CP obligatoire et demandé
                cp = bien["code_postal"]
                if cp[:2] not in cibles:
                    continue
                p = bien.get("prix") or 0
                s = bien.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue
                biens.append(bien)

            page += 1
            await asyncio.sleep(_jitter(0.5))

    print(f"[{LABEL}] {len(biens)} annonces")
    return biens


if __name__ == "__main__":
    standalone_main(search, AGENCE)

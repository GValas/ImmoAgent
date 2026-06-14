"""scrapers/agorastore.py — Agorastore (ventes immobilières des collectivités / État)

Méthode : scrape_simple (httpx) — SSR : objets produit en JSON INLINE dans le HTML
URL : https://www.agorastore.fr/ventes-immobilieres/{region-slug}
       (redirige 301 vers agorastore-immo.fr mais sert le même HTML SSR avec le JSON)
       → on parcourt les régions couvrant les départements cibles, PUIS post-filtre
         STRICT sur le code département `(NN)` extrait du `productName`.
Cartes : objets JSON `{"productName": "...", "sale": {"currentPrice": ...},
         "productPageUrl": "...", ...}` repérés par regex dans le HTML brut.
Particularités :
  - Ventes aux enchères de biens publics (État, collectivités) → stock national
    modeste mais réel ; le prix est l'enchère COURANTE (`currentPrice`).
  - Le `productName` encode « {Type} - {Surface} m² - {Ville} ({NN}) » → on en tire
    type / surface / ville / département. Le CP exact n'est qu'en page détail.
  - `hidePrice:true` ou `currentPrice:null` possible → prix=None.

Interface : async def search(criteres: dict) -> list[dict]
"""
import asyncio
import re

from scrapers._base import get_with_retry, make_client

BASE_URL = "https://www.agorastore.fr"
MAX_PAGES = 5

# Régions administratives → départements cibles couverts.
REGION_SLUGS = {
    "centre-val-de-loire": {"28", "36", "37", "41", "45", "18"},
    "pays-de-la-loire": {"49", "53", "72"},
    "bourgogne-franche-comte": {"58", "89"},
}

_NAME_TOKEN = re.compile(r'"productName":"([^"]+)"')
_NAME_RE = re.compile(r"^(?P<type>[^-]+?)\s*-\s*(?P<surf>[\d\s\xa0]+)\s*m²\s*-\s*"
                      r"(?P<ville>.+?)\s*\((?P<dept>\d{2,3})\)\s*$")
_KEEP_TYPE = re.compile(r"maison|propri|villa|ferme|longere|longère|manoir|"
                        r"chateau|château|demeure|domaine|moulin", re.IGNORECASE)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    surface_min = criteres.get("surface_min", 0)
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    results: list[dict] = []
    seen: set[str] = set()

    async with make_client() as client:
        for region, region_depts in REGION_SLUGS.items():
            if not (region_depts & departements):
                continue
            for page in range(1, MAX_PAGES + 1):
                url = f"{BASE_URL}/ventes-immobilieres/{region}?page={page}"
                r = await get_with_retry(client, url)
                if r is None or r.status_code != 200:
                    break
                biens = _parse_page(r.text, departements, seen)
                if not biens:
                    break
                for b in biens:
                    s = b.get("surface") or 0
                    p = b.get("prix") or 0
                    if surface_min and s and s < surface_min:
                        continue
                    if prix_max and p and p > prix_max:
                        continue
                    if prix_min and p and p < prix_min:
                        continue
                    results.append(b)
                print(f"[Agorastore] {region} p{page}: {len(biens)} cartes (zone)")
                await asyncio.sleep(0.5)
            await asyncio.sleep(0.6)

    return results


def _parse_page(html: str, departements: set[str], seen: set[str]) -> list[dict]:
    biens: list[dict] = []
    # Chaque objet produit commence par "productName" ; on borne la fenêtre de
    # recherche des autres champs jusqu'au "productName" SUIVANT (objets non
    # imbriqués) pour éviter qu'un regex glouton ne fusionne deux annonces.
    matches = list(_NAME_TOKEN.finditer(html))
    for i, m in enumerate(matches):
        name = m.group(1)
        nm = _NAME_RE.match(name)
        if not nm:
            continue
        dept = nm.group("dept")[:2]
        if dept not in departements:
            continue
        if not _KEEP_TYPE.search(nm.group("type")):
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        window = html[m.end():end]

        url_m = re.search(r'"productPageUrl":"([^"]*)"', window)
        href = url_m.group(1) if url_m else ""
        if href and not href.startswith("http"):
            href = "https://www.agorastore-immo.fr" + href
        id_m = re.search(r"-(\d+)\.aspx", href)
        id_annonce = id_m.group(1) if id_m else (href or name)
        if id_annonce in seen:
            continue
        seen.add(id_annonce)

        surf_raw = re.sub(r"[\s\xa0]", "", nm.group("surf"))
        try:
            surface = float(surf_raw) if surf_raw else None
        except ValueError:
            surface = None
        price_m = re.search(r'"currentPrice":([\d.]+|null)', window)
        prix = None
        if price_m and price_m.group(1) != "null":
            prix = float(price_m.group(1))
        img_m = re.search(r'"productImageMediumSizeUrl":"([^"]*)"', window)
        photos = [img_m.group(1)] if img_m and img_m.group(1) else []

        biens.append({
            "source": "agorastore",
            "url": href,
            "id_annonce": str(id_annonce),
            "titre": name[:150],
            "type_bien": nm.group("type").strip().lower(),
            "description": "",
            "departement": dept,
            "ville": nm.group("ville").strip()[:80],
            "code_postal": "",  # CP exact seulement en page détail
            "surface": surface,
            "surface_terrain": None,
            "pieces": None,
            "chambres": None,
            "prix": prix,
            "photos": photos,
            "dpe": None,
            "agence": "Agorastore (enchères publiques)",
        })
    return biens


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Agorastore")

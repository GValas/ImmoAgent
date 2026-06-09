"""scrapers/info_encheres.py — Info Enchères (ventes aux enchères immobilières judiciaires)

Méthode : scrape_simple (httpx) — SSR HTML pur (aucun JS requis).
Site national de ventes aux enchères immobilières (adjudications judiciaires).

Liste : /recherche.php?1=1&cat=1&snr={page}   (snr = 0,1,2... ; ~20 annonces/page)
  → PAS de filtre département serveur fiable : le <select name="departement"> est
    peuplé en xajax (ID internes ≠ code dept réel ; departement=45 renvoie le Lot 46).
  → On scrape donc l'inventaire national (volume faible, ~50 annonces) et on
    POST-FILTRE STRICT sur le code département, qui apparaît de façon fiable :
      - colonne dédiée de la table liste (3ᵉ <td> = "01", "69"...) ;
      - et confirmé par le slug d'URL « ...-{ville}-{DD}-ref-N.html ».

Table liste : table.liste > tr (1 tr = 1 annonce), colonnes :
  0 ref | 1 ville (titre) | 2 code dept (DD) | 3 nature | 4 mise à prix | 5 date vente | 6 notaire

Page détail (...-ref-N.html) : table label/valeur enrichit le bien :
  "Référence :" | "Nature du bien :" | "Adresse :" (→ CP réel + ville) |
  "Superficie :" (m²) | "Mise à prix" | "Vente le :" | "Au Tribunal Judiciaire de :"
  → Pas de photos exploitables (annonces judiciaires majoritairement textuelles).

Type de bien : colonne « nature » (Maison / Villa / Appartement / Studio / Terrain /
  Immeuble / Local...). On ne conserve que l'habitation (maison/villa/...).

Couverture : inventaire national faible et très variable géographiquement (souvent
  concentré dans le Sud) ; sur une zone donnée le stock peut être nul à un instant T,
  mais le scraper reste fonctionnel.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.info-encheres.com"
MAX_PAGES = 6          # snr 0..5 (large marge ; le stock réel tient en ~3 pages)
ENRICH_DETAIL = True   # fetch fiche détail pour CP/surface/description

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Natures (colonne liste) à conserver : habitation
_KEEP_TYPE = re.compile(
    r"maison|villa|appartement|studio|propri[eé]t[eé]|ferme|long[eè]re|manoir|"
    r"ch[aâ]teau|moulin|demeure|domaine|mas|g[iî]te|duplex|loft",
    re.IGNORECASE,
)
# Natures explicitement exclues
_EXCLUDE_TYPE = re.compile(
    r"terrain|parcelle|garage|parking|local|commerc|immeuble|hangar|industriel|"
    r"bureau|cave|fonds|droits",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        # 1) Collecte de l'inventaire national (pagination snr)
        listings: list[dict] = []
        for page in range(MAX_PAGES):
            url = f"{BASE_URL}/recherche.php?1=1&cat=1&snr={page}"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[InfoEncheres] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            rows = _parse_listing(r.text)
            if not rows:
                break

            new = 0
            for row in rows:
                if row["id_annonce"] in seen:
                    continue
                seen.add(row["id_annonce"])
                listings.append(row)
                new += 1
            if new == 0:
                break
            await asyncio.sleep(0.5)

        print(f"[InfoEncheres] {len(listings)} annonces nationales collectées")

        # 2) Post-filtre département STRICT + type, puis enrichissement détail
        for row in listings:
            dept = row["departement"]
            if dept not in departements:
                continue

            nature = row.get("type_bien") or ""
            if _EXCLUDE_TYPE.search(nature) and not _KEEP_TYPE.search(nature):
                continue
            if not _KEEP_TYPE.search(nature):
                continue

            bien = dict(row)
            if ENRICH_DETAIL:
                try:
                    detail = await _scrape_detail(client, bien["url"])
                    bien.update({k: v for k, v in detail.items() if v is not None})
                    await asyncio.sleep(0.4)
                except Exception as e:
                    print(f"[InfoEncheres] détail KO {bien['id_annonce']}: {e}")

            # Re-vérification stricte du département via le CP enrichi (0 fuite)
            cp = bien.get("code_postal") or ""
            if cp and cp[:2] != dept:
                # incohérence liste/détail → on fait confiance au CP réel
                if cp[:2] not in departements:
                    continue
                bien["departement"] = cp[:2]
                dept = cp[:2]

            # Bornes prix / surface (sans exclure si champ manquant)
            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue

            results.append(bien)

    # Récap par département (aide au repérage de fuite)
    vus: dict[str, int] = {}
    for b in results:
        vus[b["departement"]] = vus.get(b["departement"], 0) + 1
    if vus:
        print(f"[InfoEncheres] Retenus par dept : {vus}")

    return results


def _parse_listing(html: str) -> list[dict]:
    """Parse table.liste → un dict par annonce (champs liste)."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for table in soup.select("table.liste"):
        for tr in table.select("tr"):
            link = tr.select_one("a[href*='ref-']")
            if not link:
                continue
            tds = tr.select("td")
            if len(tds) < 5:
                continue

            href = link.get("href", "")
            url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

            # Département : colonne dédiée (3ᵉ td) avec repli sur le slug d'URL
            dept = ""
            if len(tds) > 2:
                m = re.search(r"\b(\d{2,3})\b", tds[2].get_text(strip=True))
                if m:
                    dept = m.group(1).zfill(2)[:2] if len(m.group(1)) <= 3 else ""
            if not dept:
                m = re.search(r"-(\d{2,3})-ref-", href)
                if m:
                    dept = m.group(1)[:2].zfill(2)

            ref = tds[0].get_text(strip=True)
            id_annonce = ref or _id_from_href(href)

            ville = tds[1].get_text(" ", strip=True)
            nature = tds[3].get_text(" ", strip=True) if len(tds) > 3 else ""
            prix = _parse_price(tds[4].get_text(" ", strip=True)) if len(tds) > 4 else None

            out.append(
                {
                    "source": "info_encheres",
                    "url": url,
                    "id_annonce": id_annonce,
                    "titre": (f"{nature} {ville}".strip())[:150] or ville[:150],
                    "type_bien": nature.lower(),
                    "description": "",
                    "departement": dept,
                    "ville": ville.title()[:80],
                    "code_postal": "",
                    "surface": None,
                    "surface_terrain": None,
                    "pieces": None,
                    "chambres": None,
                    "prix": prix,
                    "photos": [],
                    "dpe": None,
                    "agence": None,
                }
            )
    return out


async def _scrape_detail(client: httpx.AsyncClient, url: str) -> dict:
    """Fiche détail : table label/valeur → CP, ville, surface, description, notaire."""
    r = await client.get(url)
    if r.status_code != 200:
        return {}
    soup = BeautifulSoup(r.text, "html.parser")

    fields: dict[str, str] = {}
    for table in soup.select("table"):
        for tr in table.select("tr"):
            tds = tr.select("td")
            if len(tds) == 2:
                label = tds[0].get_text(" ", strip=True).rstrip(":").strip().lower()
                value = tds[1].get_text(" ", strip=True)
                if label and value and label not in fields:
                    fields[label] = value

    out: dict = {}

    adresse = fields.get("adresse", "")
    cp = ville = None
    m = re.search(r"\b(\d{5})\b", adresse)
    if m:
        cp = m.group(1)
        # ville = ce qui suit le CP (le dernier mot/segment est souvent le dept en majuscules)
        after = adresse[m.end():].strip()
        if after and not re.match(r"^[A-ZÀ-Ÿ\s\-]+$", after):
            ville = after
    if cp:
        out["code_postal"] = cp
        out["departement"] = cp[:2]
    if ville:
        out["ville"] = ville.title()[:80]

    sup = fields.get("superficie", "")
    out["surface"] = _parse_surface(sup)

    if "mise à prix" in fields:
        out["prix"] = _parse_price(fields["mise à prix"]) or out.get("prix")

    nat = fields.get("nature du bien", "")
    if nat:
        out["type_bien"] = nat.lower()

    # Description : corps de texte de la fiche (paragraphes hors table label/valeur)
    desc_parts: list[str] = []
    content = soup.select_one("div.content") or soup
    txt = content.get_text("\n", strip=True)
    m_desc = re.search(
        r"(D[ée]signation|Descriptif|Description)\s*[:\-]?\s*(.{40,1500})",
        txt,
        re.IGNORECASE | re.DOTALL,
    )
    if m_desc:
        desc_parts.append(re.sub(r"\s+", " ", m_desc.group(2)).strip())
    if desc_parts:
        out["description"] = desc_parts[0][:1200]

    # Notaire / avocat / tribunal comme agence (mandataire de la vente)
    agence = fields.get("au tribunal judiciaire de", "") or fields.get("avocat", "")
    if agence:
        out["agence"] = agence.split("  ")[0].strip()[:120]

    return out


# ── Helpers ──────────────────────────────────────────────────────────────────

def _id_from_href(href: str) -> str:
    m = re.search(r"ref-(\d+)\.html", href)
    if m:
        return m.group(1)
    m = re.match(r"^(\d+)-", href)
    return m.group(1) if m else href


def _parse_price(text: str) -> float | None:
    """'50.000€', '50 000 € Outre frais...' → 50000.0.

    On capture le 1ᵉʳ nombre suivi de € (séparateurs milliers . ou espace),
    pour éviter d'avaler le texte légal qui suit (« ...article 1277... »).
    """
    if not text:
        return None
    m = re.search(r"([\d][\d\s\xa0. ]*)\s*€", text)
    if not m:
        # repli : 1er groupe de chiffres avec séparateurs de milliers
        m = re.search(r"(\d[\d\s\xa0. ]{2,})", text)
        if not m:
            return None
    digits = re.sub(r"[^\d]", "", m.group(1))
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


def _parse_surface(text: str) -> float | None:
    """'181.60 m²' → 181.6"""
    if not text:
        return None
    m = re.search(r"([\d\s\xa0]+(?:[.,]\d+)?)\s*m", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", ".")
    try:
        f = float(val)
        return f if 5 <= f <= 5000 else None
    except ValueError:
        return None


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
    print(f"\nTotal Info Enchères : {len(biens)} annonces")
    depts = sorted({(b["code_postal"][:2] if b["code_postal"] else b["departement"]) for b in biens})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal'] or b['departement']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

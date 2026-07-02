"""scrapers/cabinetderocquigny.py — Cabinet de Rocquigny (agence indépendante Orléans / Loiret)

Méthode : scrape_simple (httpx) — SSR HTML (WordPress, hydratation JS légère
          mais contenu présent dans le HTML brut).
URL liste : /logements/?type=vente  puis  /logements/page/{N}/?type=vente
            → pas de filtre département serveur : agence MONO-LOIRET (45),
              implantée Orléans / Sologne / Loiret. Post-filtre STRICT sur le
              code postal de la page détail (CP[:2] == "45").

Cartes liste : div.item-logement
  - URL    : a[href*="/logement/"]  → /logement/{slug}/
  - Titre  : <h2>/<h3> ou texte de la carte
  - Ville  : libellé court ("Orleans", "Olivet", "Tigy"…)
  - Surface: "… - NNN m²"
  - Chambres: "N chambres"
  - Prix   : "845 000 €"

Page détail (/logement/{slug}/) : fournit le code postal (ex. 45000), la
  surface du terrain ("Surface du terrain NNN m²"), la description complète et
  les photos (wp-content/uploads). Le CP n'apparaît pas sur la carte → on
  requête le détail pour les biens candidats (filtre prix/surface appliqué
  d'abord sur la carte pour limiter les requêtes).

Filtre département : agence mono-45 ; on ne RETIENT un bien que si CP[:2] == "45"
  ET que 45 fait partie des départements ciblés. → 0 fuite hors-zone.

Volume observé : ~10-12 biens à la vente (toutes catégories), dont quelques
  maisons/propriétés. Petit stock mais réel.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from scrapers._base import HEADERS

BASE_URL = "https://www.cabinetderocquigny.com"
MAX_PAGES = 6
PHOTOS_PER_CARD = 10
DEPT = "45"  # agence mono-Loiret


# Types de bien à conserver / exclure (détectés dans titre + slug d'URL)
_KEEP_TYPE = re.compile(
    r"maison|propriete|propriété|villa|ferme|longere|longère|manoir|chateau|"
    r"château|moulin|demeure|domaine|mas|corps[- ]de[- ]ferme|bourgeoise",
    re.IGNORECASE,
)
_EXCLUDE_TYPE = re.compile(
    r"appartement|\bt[0-9]\b|studio|terrain|local|commerce|garage|parking|"
    r"immeuble|bureau|fonds|location|loue",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    if DEPT not in departements:
        # L'agence ne couvre que le Loiret : si 45 n'est pas ciblé, rien à faire.
        print(f"[Rocquigny] Dept {DEPT} hors zone ciblée → 0 annonce")
        return []

    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)

    results: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=25
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            if page == 1:
                url = f"{BASE_URL}/logements/?type=vente"
            else:
                url = f"{BASE_URL}/logements/page/{page}/?type=vente"
            try:
                r = await client.get(url)
            except Exception as e:
                print(f"[Rocquigny] Erreur page {page}: {e}")
                break
            if r.status_code != 200:
                break

            cards = _extract_cards(r.text)
            if not cards:
                break

            new_on_page = 0
            for card in cards:
                pre = _parse_card(card)
                if not pre:
                    continue
                if pre["url"] in seen:
                    continue
                seen.add(pre["url"])

                # Pré-filtre prix/surface sur les champs de la carte (évite des
                # requêtes détail inutiles) sans exclure si champ manquant.
                p = pre.get("prix") or 0
                s = pre.get("surface") or 0
                if prix_max and p and p > prix_max:
                    continue
                if prix_min and p and p < prix_min:
                    continue
                if surface_min and s and s < surface_min:
                    continue

                new_on_page += 1
                try:
                    bien = await _enrich_detail(client, pre)
                except Exception as e:
                    print(f"[Rocquigny] Erreur détail {pre['url']}: {e}")
                    continue
                if not bien:
                    continue

                # Filtre département STRICT (mono-45) : on exige CP[:2] == "45".
                if not bien["code_postal"] or bien["code_postal"][:2] != DEPT:
                    continue

                results.append(bien)
                await asyncio.sleep(0.5)

            if new_on_page == 0:
                break
            await asyncio.sleep(0.5)

    print(f"[Rocquigny] Dept {DEPT}: {len(results)} annonces")
    return results


def _extract_cards(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.item-logement")
    if cards:
        return cards
    # Repli : conteneurs autour des liens /logement/
    out = []
    for a in soup.select('a[href*="/logement/"]'):
        c = a
        for _ in range(5):
            c = c.parent
            if c is None:
                break
            cls = " ".join(c.get("class", []))
            if "item-logement" in cls or c.name in ("article", "li"):
                out.append(c)
                break
    return out


def _parse_card(card) -> dict | None:
    link = card.select_one('a[href*="/logement/"]')
    if not link:
        return None
    href = link.get("href", "")
    if not href:
        return None
    url = href if href.startswith("http") else BASE_URL + href

    text = card.get_text(" ", strip=True)

    # Type : exclure location / appartement / terrain via titre+slug
    blob = f"{url} {text}"
    if _EXCLUDE_TYPE.search(blob) and not _KEEP_TYPE.search(blob):
        return None

    # Titre
    title_el = card.find(["h2", "h3"])
    titre = (
        title_el.get_text(" ", strip=True)
        if title_el
        else text.split("  ")[0]
    )
    titre = re.sub(r"\s+", " ", titre).strip()

    # Prix
    prix = _parse_price(text)

    # Surface habitable : "- NNN m²"
    surface = None
    m_s = re.search(r"(\d[\d\s\xa0]*)\s*m²", text)
    if m_s:
        surface = _to_float(m_s.group(1))

    # Chambres
    chambres = None
    m_c = re.search(r"(\d+)\s*chambre", text, re.IGNORECASE)
    if m_c:
        chambres = int(m_c.group(1))

    return {
        "url": url,
        "titre": titre[:150],
        "prix": prix,
        "surface": surface,
        "chambres": chambres,
    }


async def _enrich_detail(client: httpx.AsyncClient, pre: dict) -> dict | None:
    r = await client.get(pre["url"])
    if r.status_code != 200:
        return None
    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    # Code postal : premier CP plausible dans le texte de la fiche
    code_postal = ""
    for cp in re.findall(r"\b(\d{5})\b", html):
        if cp[:2] == DEPT:
            code_postal = cp
            break
    if not code_postal:
        m_any = re.search(r"\b(\d{5})\b", html)
        code_postal = m_any.group(1) if m_any else ""

    # Ville : depuis le slug d'URL ou le titre (les communes du Loiret).
    ville = _ville_from(pre["url"], pre["titre"])

    # Description
    description = ""
    cand = soup.find(
        string=re.compile(r"Cette\s+(maison|propri|demeure|villa)", re.IGNORECASE)
    )
    if cand:
        description = cand.strip()
    if not description:
        # repli : meta description
        md = soup.find("meta", attrs={"name": "description"})
        if md:
            description = md.get("content", "")

    # Surface (si absente de la carte) : "- NNN m²" dans le titre détail
    surface = pre.get("surface")
    if surface is None:
        m_s = re.search(r"(\d[\d\s\xa0]*)\s*m²", soup.get_text(" ", strip=True))
        if m_s:
            surface = _to_float(m_s.group(1))

    # Surface terrain : "Surface du terrain NNN m²"
    surface_terrain = None
    txt = soup.get_text(" ", strip=True)
    m_t = re.search(
        r"Surface\s+du\s+terrain\s*([\d\s\xa0]+)\s*m²", txt, re.IGNORECASE
    )
    if m_t:
        surface_terrain = _to_float(m_t.group(1))

    # Type de bien depuis le titre
    type_bien = "maison"
    mt = _KEEP_TYPE.search(pre["titre"])
    if mt:
        type_bien = mt.group(0).lower()

    # id_annonce : slug final
    slug = [p for p in pre["url"].split("/") if p][-1]
    id_annonce = slug or pre["url"]

    # Photos
    photos = []
    for img in soup.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
        if "wp-content/uploads" in src and not src.startswith("data:"):
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE_URL + src
            if src not in photos:
                photos.append(src)
    photos = photos[:PHOTOS_PER_CARD]

    return {
        "source": "cabinetderocquigny",
        "url": pre["url"],
        "id_annonce": id_annonce,
        "titre": pre["titre"],
        "type_bien": type_bien,
        "description": description[:1200],
        "departement": DEPT,
        "ville": ville[:80],
        "code_postal": code_postal,
        "surface": surface,
        "surface_terrain": surface_terrain,
        "pieces": None,
        "chambres": pre.get("chambres"),
        "prix": pre.get("prix"),
        "photos": photos,
        "dpe": None,
        "agence": "Cabinet de Rocquigny",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ville_from(url: str, titre: str) -> str:
    """Déduit la commune depuis le slug ou le titre (communes du Loiret)."""
    known = [
        "Orleans", "Orléans", "Olivet", "Tigy", "Sandillon", "Saint-Jean-de-Braye",
        "Saint-Jean-de-la-Ruelle", "Fleury-les-Aubrais", "Checy", "Checy",
        "Saint-Denis-en-Val", "La Source", "Jargeau", "Sully-sur-Loire",
        "Beaugency", "Meung-sur-Loire", "Gien", "Montargis", "Pithiviers",
        "Chateauneuf-sur-Loire", "Châteauneuf-sur-Loire", "Combleux",
    ]
    blob = f"{url} {titre}".lower()
    for v in known:
        if v.lower() in blob:
            return v
    # repli : dernier mot capitalisé du titre
    words = re.findall(r"[A-ZÀ-Ý][a-zà-ÿ-]+", titre)
    return words[-1] if words else ""


def _parse_price(text: str) -> float | None:
    m = re.search(r"([\d][\d\s\xa0.]{3,})\s*€", text)
    if not m:
        return None
    val = re.sub(r"[\s\xa0.]", "", m.group(1))
    try:
        return float(val) if val else None
    except ValueError:
        return None


def _to_float(s: str) -> float | None:
    val = re.sub(r"[\s\xa0]", "", s)
    try:
        f = float(val)
        return f
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
    print(f"\nTotal Cabinet de Rocquigny: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens if b["code_postal"]})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:55]}"
            f" — {b['prix']}€"
            f" — {b.get('surface') or '?'}m²"
            f" — terrain {b.get('surface_terrain') or '?'}m²"
            f" — {b['type_bien']} — {b['ville']}"
        )

"""scrapers/imkiz.py — imkiz (agence immobilière en ligne, tarif fixe)

Méthode : scrape_simple (httpx) — inventaire national via sitemap XML + fiches SSR.

imkiz n'a pas de page de listing filtrable par département en SSR (la recherche
est une carte Google Maps qui sauve une session puis redirige). En revanche le
sitemap https://www.imkiz.com/sitemap_properties.xml expose les ~3700 fiches
avec, dans le slug, le code postal et le prix. On extrait le code postal du slug,
on post-filtre par dept (cp[:2] ∈ departements, comme remax/era), puis on
récupère chaque fiche en SSR pour les données fiables.

Fiche /immo/{id}/a-vendre-{type}-{n}-pieces-{n}-chambres-{ville}-{cp}-{prix}-euro :
  - <title>  : "Vente à {Ville} ({CP}) {prix} € - {Type} {n} pieces {n} chambres"
  - Bloc "Informations clés" : pièces, chambres, surface habitable/carrez, DPE, étage
  - JSON-LD Product : prix, description, image principale
  - Photos : https://images.imkiz.com/{folder}/{file}.jpg

Filtre dept : post-filtre par code_postal[:2] (voie b). Vérifié : ~39 fiches
en-département sur l'inventaire national.

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio
import html
import re

import httpx

BASE_URL = "https://www.imkiz.com"
SITEMAP_URL = f"{BASE_URL}/sitemap_properties.xml"

MAX_DETAILS = 80          # plafond de fiches détaillées récupérées (sécurité)
CONCURRENCY = 6
PHOTOS_PER_AD = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_TITLE_RE = re.compile(
    r"à\s+(?P<ville>.+?)\s*\((?P<cp>\d{5})\)\s*(?P<prix>.*?)\s*-\s*"
    r"(?P<type>Maison|Appartement)\s+(?P<pieces>\d+)\s*pieces?"
    r"(?:\s+(?P<chambres>\d+)\s*chambres?)?",
    re.IGNORECASE,
)


async def search(criteres: dict) -> list[dict]:
    departements = {str(d).zfill(2) for d in criteres.get("departements", [])}
    prix_max = criteres.get("prix_max") or 0
    prix_min = criteres.get("prix_min") or 0
    surface_min = criteres.get("surface_min") or 0

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=30
    ) as client:
        # 1) Sitemap → URLs de fiches "à vendre" dans les départements cibles
        try:
            r = await client.get(SITEMAP_URL)
            r.raise_for_status()
            locs = re.findall(r"<loc>([^<]+)</loc>", r.text)
        except Exception as e:
            print(f"[Imkiz] Erreur sitemap : {e}")
            return []

        candidates: list[tuple[str, str]] = []  # (cp, url)
        for loc in locs:
            if "/immo/" not in loc or "/a-louer-" in loc:
                continue
            cp = _extract_cp(loc)
            if cp and cp[:2] in departements:
                candidates.append((cp, loc))

        # dédup URL
        seen_urls = set()
        candidates = [
            (cp, u) for cp, u in candidates
            if not (u in seen_urls or seen_urls.add(u))
        ]
        candidates = candidates[:MAX_DETAILS]
        print(f"[Imkiz] {len(candidates)} fiches en-département dans le sitemap")

        # 2) Récupération des fiches en parallèle (concurrence limitée)
        sem = asyncio.Semaphore(CONCURRENCY)

        async def fetch(cp: str, url: str) -> dict | None:
            async with sem:
                try:
                    rr = await client.get(url)
                    rr.raise_for_status()
                    return _parse_detail(rr.text, url, cp)
                except Exception:
                    return None

        biens_raw = await asyncio.gather(
            *(fetch(cp, u) for cp, u in candidates)
        )

    # 3) Filtre prix/surface + dédup
    results: list[dict] = []
    seen_ids = set()
    for b in biens_raw:
        if not b:
            continue
        if b["departement"] not in departements:
            continue
        p = b.get("prix") or 0
        s = b.get("surface") or 0
        if prix_max and p and p > prix_max:
            continue
        if prix_min and p and p < prix_min:
            continue
        if surface_min and s and s < surface_min:
            continue
        aid = b["id_annonce"]
        if aid in seen_ids:
            continue
        seen_ids.add(aid)
        results.append(b)

    by_dept: dict[str, int] = {}
    for b in results:
        by_dept[b["departement"]] = by_dept.get(b["departement"], 0) + 1
    for dept, n in sorted(by_dept.items()):
        print(f"[Imkiz] Dept {dept}: {n} annonces")

    return results


def _extract_cp(url: str) -> str | None:
    """Code postal depuis le slug de la fiche.

    Formats : ...-{cp}-{prix}-euro  |  ...-{cp}  |  fallback : 1er 5-chiffres.
    On évite de confondre le prix (5 chiffres) avec le code postal.
    """
    slug = url.rsplit("/", 1)[-1]
    m = re.search(r"-(\d{5})-\d+-euro$", slug)
    if m:
        return m.group(1)
    m = re.search(r"-(\d{5})$", slug)
    if m:
        return m.group(1)
    cps = re.findall(r"(?<!\d)(\d{5})(?!\d)", slug)
    return cps[0] if cps else None


def _parse_detail(page: str, url: str, cp_slug: str) -> dict | None:
    try:
        flat = html.unescape(re.sub(r"<[^>]+>", " ", page))
        flat = re.sub(r"\s+", " ", flat)

        # ── Titre → ville, cp, prix, type, pièces, chambres ──
        mt = re.search(r"<title>(.*?)</title>", page, re.S)
        title = html.unescape(mt.group(1).strip()) if mt else ""
        ville = code_postal = type_bien = ""
        prix = pieces = chambres = None

        mtt = _TITLE_RE.search(title)
        if mtt:
            ville = mtt.group("ville").strip()
            code_postal = mtt.group("cp")
            type_bien = mtt.group("type").lower()
            pieces = _to_int(mtt.group("pieces"))
            chambres = _to_int(mtt.group("chambres"))
            prix = _parse_price(mtt.group("prix"))
        if not code_postal:
            code_postal = cp_slug

        dept = code_postal[:2]

        # ── Bloc "Informations clés" (plus fiable pour surface/DPE/étage) ──
        info = ""
        mi = re.search(r"Informations clés(.*?)(?:Taxe foncière|Lots de copro|$)", flat)
        if mi:
            info = mi.group(1)

        if pieces is None:
            pieces = _grab_int(r"Nombre de pièces\s*:\s*(\d+)", info or flat)
        if chambres is None:
            chambres = _grab_int(r"Nombre de chambres\s*:\s*(\d+)", info or flat)

        surface = _grab_float(
            r"Surface (?:habitable|carrez|loi carrez)\s*:\s*([\d,\.]+)", info or flat
        )
        etage = _grab_int(r"Etage\s*:\s*(\d+)", info or flat)
        dpe = None
        md = re.search(r"DPE \(note\)\s*:\s*([A-G])", info or flat)
        if md:
            dpe = md.group(1)

        # ── Prix / surface terrain depuis JSON-LD + slug si besoin ──
        if prix is None:
            mp = re.search(r'"price"\s*:\s*"?(\d+(?:\.\d+)?)"?', page)
            if mp:
                prix = float(mp.group(1))
        if prix is None:
            # dernier recours : prix dans le slug (...-{cp}-{prix}-euro)
            ms = re.search(r"-\d{5}-(\d+)-euro$", url.rsplit("/", 1)[-1])
            if ms:
                prix = float(ms.group(1))

        description = ""
        mdesc = re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', page)
        if mdesc:
            description = html.unescape(
                mdesc.group(1).encode().decode("unicode_escape", errors="ignore")
            ).strip()

        surface_terrain = _grab_float(
            r"terrain d[’'e]\s*environ\s*([\d\s]+)\s*m²", description
        ) or _grab_float(r"terrain de\s*([\d\s]+)\s*m²", description)

        if not type_bien:
            tl = (title + " " + url).lower()
            type_bien = "appartement" if "appartement" in tl else "maison"

        # ── Photos ──
        photos = []
        for m in re.finditer(r"https://images\.imkiz\.com/[\w/]+\.(?:jpg|jpeg|png|webp)", page):
            u = m.group(0)
            if u not in photos:
                photos.append(u)
            if len(photos) >= PHOTOS_PER_AD:
                break

        # ── id annonce depuis l'URL /immo/{id}/ ──
        mid = re.search(r"/immo/(\w+)/", url)
        id_annonce = mid.group(1) if mid else url.rsplit("/", 1)[-1]

        if not title:
            title = f"{type_bien.capitalize()} {ville}".strip()

        return {
            "source": "imkiz",
            "url": url,
            "id_annonce": id_annonce,
            "titre": title[:180],
            "type_bien": type_bien,
            "description": description[:1500] if description else None,
            "departement": dept,
            "ville": ville[:80] if ville else None,
            "code_postal": code_postal,
            "surface": surface,
            "surface_terrain": surface_terrain,
            "pieces": pieces,
            "chambres": chambres,
            "prix": prix,
            "dpe": dpe,
            "etage": etage,
            "photos": photos,
            "agence": "imkiz",
        }
    except Exception:
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_int(v) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _grab_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _grab_float(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    raw = re.sub(r"\s", "", m.group(1)).replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_price(text: str) -> float | None:
    """'210 000 €' → 210000.0 ; 'prix : nous contacter' → None"""
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text.split("€")[0])
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


# ── CLI standalone ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    biens = asyncio.run(
        search({
            "departements": criteres.departements,
            "prix_max": criteres.prix_max,
            "prix_min": getattr(criteres, "prix_min", 0),
            "surface_min": criteres.surface_min,
        })
    )
    print(f"\nTotal imkiz: {len(biens)} annonces")
    depts = sorted({b["departement"] for b in biens})
    print(f"Départements vus: {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['departement']}] {b['titre'][:60]}"
            f" — {b['prix']}€ — {b.get('surface')}m²"
            f" — {b['ville']} ({b['code_postal']})"
            f" — DPE {b.get('dpe')} — {len(b['photos'])} photos"
        )

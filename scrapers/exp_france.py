"""scrapers/exp_france.py — eXp France (réseau de mandataires)

Méthode : api_inoff — backend Supabase (PostgREST) du site Next.js expfrance.fr.
La page /search-properties filtre côté client en interrogeant directement
la table `listings` de Supabase avec la clé anon publique (extraite des chunks JS).

Filtre département : SERVEUR — `zipcode=like.{dept}%` sur PostgREST. Vérifié :
chaque bien renvoyé a réellement un code postal commençant par le dept ciblé.

Endpoint  : https://ywzpnbmomlzkcbzzkaqr.supabase.co/rest/v1/listings
Filtres    : country_code=eq.FR, status=eq.1, property_type=in.(Maison,...),
             zipcode=like.{dept}%, price<=, square_feet>=
URL fiche  : https://www.expfrance.fr/property/{id}
Photos     : champ `images[].url` (CDN supabase)

Interface : async def search(criteres: dict) -> list[dict]
"""

import asyncio

import httpx

SITE_URL = "https://www.expfrance.fr"
SUPABASE_URL = "https://ywzpnbmomlzkcbzzkaqr.supabase.co"
REST_URL = f"{SUPABASE_URL}/rest/v1/listings"

# Clé anon publique (rôle anon, lecture seule) — extraite des chunks JS du site.
ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inl3enBuYm1vbWx6a2NienprYXFyIiwicm9s"
    "ZSI6ImFub24iLCJpYXQiOjE3NDQ2NDkyMzMsImV4cCI6MjA2MDIyNTIzM30."
    "6b8PT7DMzY2jnRgglammdCpqsT6EKR1_Na2T7djGb9A"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "apikey": ANON_KEY,
    "Authorization": f"Bearer {ANON_KEY}",
    "Accept-Profile": "public",
}

# property_type retenus comme "maison / propriété"
HOUSE_TYPES = ["Maison", "Villa", "Propriété", "Château", "Ferme", "Immeuble"]

PAGE_SIZE = 100
MAX_PAGES = 5  # plafond — l'inventaire FR par dept est petit (<100 le plus souvent)


async def search(criteres: dict) -> list[dict]:
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max")
    surface_min = criteres.get("surface_min")

    results: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True) as client:
        for dept in departements:
            try:
                rows = await _fetch_dept(client, dept, prix_max, surface_min)
            except Exception as e:
                print(f"[ExpFrance] Erreur dept {dept}: {e}")
                continue

            kept = 0
            for row in rows:
                # Garde-fou : on REVERIFIE le département via le code postal.
                cp = str(row.get("zipcode") or "")
                if cp[:2] != dept:
                    continue
                bien = _parse_row(row, dept, cp)
                if not bien:
                    continue
                if bien["id_annonce"] in seen:
                    continue
                seen.add(bien["id_annonce"])
                results.append(bien)
                kept += 1
            print(f"[ExpFrance] Dept {dept}: {kept} annonces")
            await asyncio.sleep(0.3)

    return results


async def _fetch_dept(
    client: httpx.AsyncClient,
    dept: str,
    prix_max,
    surface_min,
) -> list[dict]:
    rows: list[dict] = []
    type_filter = "in.(" + ",".join(f'"{t}"' for t in HOUSE_TYPES) + ")"

    for page in range(MAX_PAGES):
        params = {
            "select": (
                "id,title,description,price,currency,property_type,status,"
                "city,zipcode,address,square_feet,plot_size_sqm,total_rooms,"
                "bedrooms,bathrooms,energy_efficiency_class,images,full_payload"
            ),
            "country_code": "eq.FR",
            "status": "eq.1",
            "property_type": type_filter,
            "zipcode": f"like.{dept}%",
            "order": "price.asc",
        }
        if prix_max:
            params["price"] = f"lte.{int(prix_max)}"
        if surface_min:
            params["square_feet"] = f"gte.{int(surface_min)}"

        headers = {
            "Range-Unit": "items",
            "Range": f"{page * PAGE_SIZE}-{page * PAGE_SIZE + PAGE_SIZE - 1}",
        }
        r = await client.get(REST_URL, params=params, headers=headers)
        if r.status_code not in (200, 206):
            break
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        await asyncio.sleep(0.2)

    return rows


def _parse_row(row: dict, dept: str, cp: str) -> dict | None:
    try:
        ad_id = str(row.get("id") or "")
        if not ad_id:
            return None

        fp = row.get("full_payload") or {}

        prix = _to_float(row.get("price"))
        surface = _to_float(row.get("square_feet"))
        terrain = _to_float(row.get("plot_size_sqm")) or _to_float(fp.get("surface_land"))
        if terrain == 0:
            terrain = None

        pieces = _to_int(row.get("total_rooms")) or _to_int(fp.get("rooms"))
        chambres = _to_int(row.get("bedrooms"))
        if chambres is None:
            chambres = _to_int(fp.get("bedrooms")) or None

        dpe = row.get("energy_efficiency_class") or _dpe_from_kwh(fp.get("epc_energy"))

        ville = (row.get("city") or fp.get("city") or "").strip()

        titre = (row.get("title") or "").strip()
        if not titre:
            titre = f"Maison {ville}".strip()

        description = (row.get("description") or "").strip()

        # Coordonnées (approximatives) du bien — l'API eXp les expose ; permet la
        # pré-localisation cadastrale au lieu du repli centre-commune.
        lat = _to_float(row.get("geo_lat")) or _to_float(fp.get("latitude"))
        lon = _to_float(row.get("geo_lon")) or _to_float(fp.get("longitude"))
        if not lat or not lon:
            lat = lon = None

        photos = []
        for img in (row.get("images") or []):
            u = img.get("url") or img.get("thumbnail_url")
            if u:
                photos.append(u)

        return {
            "source": "exp_france",
            "url": f"{SITE_URL}/property/{ad_id}",
            "id_annonce": ad_id,
            "titre": titre[:200],
            "type_bien": (row.get("property_type") or "maison").lower(),
            "description": description[:1500],
            "departement": dept,
            "ville": ville[:80],
            "code_postal": cp,
            "latitude": lat,
            "longitude": lon,
            "blur_radius_m": 1000.0 if lat else None,   # eXp floute la position (~1 km)
            "surface": surface,
            "surface_terrain": terrain,
            "pieces": pieces,
            "chambres": chambres,
            "prix": prix,
            "photos": photos[:12],
            "dpe": dpe,
            "agence": "eXp France",
        }
    except Exception:
        return None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _to_float(v) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v) -> int | None:
    f = _to_float(v)
    return int(f) if f is not None else None


def _dpe_from_kwh(v) -> str | None:
    """epc_energy = consommation kWh/m²/an → lettre DPE (barème FR)."""
    kwh = _to_float(v)
    if kwh is None or kwh <= 0:
        return None
    if kwh <= 70:
        return "A"
    if kwh <= 110:
        return "B"
    if kwh <= 180:
        return "C"
    if kwh <= 250:
        return "D"
    if kwh <= 330:
        return "E"
    if kwh <= 420:
        return "F"
    return "G"


# ── CLI standalone ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    biens = asyncio.run(
        search({
            "departements": criteres.departements,
            "prix_max": criteres.prix_max,
            "surface_min": criteres.surface_min,
        })
    )
    print(f"\nTotal eXp France: {len(biens)} annonces")
    depts = sorted({b["code_postal"][:2] for b in biens})
    print(f"Départements vus: {depts}")
    for b in biens[:10]:
        print(
            f"  [{b['code_postal']}] {b['titre'][:60]}"
            f" — {b['prix']}€ — {b['surface']}m²"
            f" — {b['pieces']}p — {b['ville']} — DPE {b['dpe']}"
        )

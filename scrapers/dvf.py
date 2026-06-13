"""
scrapers/dvf.py — DVF (Demandes de Valeurs Foncières)
Source : https://files.data.gouv.fr/geo-dvf/latest/csv/YYYY/departements/{code}.csv.gz
Données : transactions immobilières passées (maisons vendues 2023-2024)

Interface standard : async def search(criteres: dict) -> list[dict]
  → Retourne toujours [] (DVF n'est pas une source d'annonces actives)

Interface utilitaire : async def get_prix_m2_reference(depts: list[str]) -> dict[str, float]
  → Calcule le prix médian €/m² par département à partir des données DVF réelles
  → Utilisé par analyst.py à la place des valeurs hardcodées
"""
import asyncio
import csv
import gzip
import io
import statistics

import httpx

BASE_URL = "https://files.data.gouv.fr/geo-dvf/latest/csv"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept-Encoding": "gzip, deflate",
}

# Cache de session pour éviter de re-télécharger pendant un pipeline
_CACHE: dict[str, float] = {}

# Année courante pour construire l'URL
_YEAR = 2024


def _parse_dvf_csv(content: bytes, dept: str) -> float | None:
    """
    Parse un CSV.gz DVF et retourne le prix médian €/m² pour les maisons vendues.
    Filtre : Vente + Maison + surface_reelle_bati > 30m² + valeur_fonciere > 50 000 €
    """
    try:
        with gzip.open(io.BytesIO(content)) as f:
            text = f.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    reader = csv.DictReader(io.StringIO(text))
    prix_m2_list = []

    for row in reader:
        try:
            if row.get("nature_mutation") != "Vente":
                continue
            if row.get("type_local") != "Maison":
                continue

            val_raw = row.get("valeur_fonciere", "").replace(",", ".")
            surf_raw = row.get("surface_reelle_bati", "").replace(",", ".")
            if not val_raw or not surf_raw:
                continue

            prix = float(val_raw)
            surf = float(surf_raw)
            if surf < 30 or prix < 50_000:
                continue

            prix_m2_list.append(prix / surf)
        except (ValueError, ZeroDivisionError):
            continue

    if len(prix_m2_list) < 10:
        return None

    return round(statistics.median(prix_m2_list), 0)


async def _fetch_dept_prix_m2(client: httpx.AsyncClient, dept: str) -> float | None:
    """Télécharge le CSV DVF d'un département et calcule le prix médian €/m²."""
    if dept in _CACHE:
        return _CACHE[dept]

    # Essai sur l'année courante puis année précédente
    for year in [_YEAR, _YEAR - 1]:
        url = f"{BASE_URL}/{year}/departements/{dept}.csv.gz"
        try:
            r = await client.get(url)
            if r.status_code == 200 and len(r.content) > 10_000:
                prix_m2 = _parse_dvf_csv(r.content, dept)
                if prix_m2 and prix_m2 > 200:
                    _CACHE[dept] = prix_m2
                    print(f"[DVF] dept={dept} {year}: prix médian maison = {prix_m2:.0f} €/m²")
                    return prix_m2
        except Exception as e:
            print(f"[DVF] ERR dept={dept} year={year}: {e}")

    return None


async def get_prix_m2_reference(depts: list[str]) -> dict[str, float]:
    """
    Calcule le prix médian €/m² des maisons vendues par département (données DVF réelles).
    Retourne {code_dept: prix_m2_median} — les départements sans données gardent une valeur de secours.

    Exemple : {"72": 1667.0, "37": 2150.0, "49": 2100.0, ...}
    """
    # Valeurs de secours si le téléchargement échoue (estimations 2024 mise à jour)
    FALLBACK = {
        "72": 1650, "28": 1950, "45": 1900, "89": 1400,
        "49": 2100, "37": 2200, "36": 1200, "18": 1300,
        "58": 1150, "41": 1800, "53": 1600,
        # Autres depts fréquents
        "06": 5200, "83": 4100, "84": 2800, "13": 3500,
        "69": 4800, "75": 10500, "92": 7200, "94": 5100,
        "33": 3800, "34": 3600, "44": 3700, "31": 3400,
    }

    result: dict[str, float] = {}
    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=60) as client:
        tasks = [_fetch_dept_prix_m2(client, d) for d in depts]
        values = await asyncio.gather(*tasks, return_exceptions=True)

    for dept, val in zip(depts, values):
        if isinstance(val, (int, float)) and val and val > 0:
            result[dept] = val
        else:
            result[dept] = FALLBACK.get(dept, 2000)
            print(f"[DVF] dept={dept}: fallback → {result[dept]} €/m²")

    return result


async def search(criteres: dict) -> list[dict]:
    """
    DVF ne contient que des transactions passées — pas d'annonces actives.
    Retourne toujours une liste vide.
    """
    return []


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    depts = ["72", "37", "49", "28", "45", "41", "53", "36", "18", "58", "89"]
    print("Téléchargement des données DVF...")
    refs = asyncio.run(get_prix_m2_reference(depts))
    print("\n=== Prix médian €/m² maison par département ===")
    for d, p in sorted(refs.items()):
        print(f"  Dept {d}: {p:.0f} €/m²")

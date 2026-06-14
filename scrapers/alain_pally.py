"""scrapers/alain_pally.py — Alain Pally / Val du Loir Immobilier (Châteaudun, 28)

Méthode : scrape_simple (httpx) — SSR HTML, CMS AC3 / immo-facile.
Couverture : Châteaudun, Cloyes-sur-le-Loir, Bonneval, Varize, secteur Chartres (28).
Listing : /annonces/transaction/Vente.html (+ pagination _____{N}).
Filtre département : CP lu en clair sur la carte (« VILLE (28xxx) ») puis
POST-FILTRE strict code_postal[:2] ∈ départements cibles → 0 fuite.

Logique factorisée dans scrapers/_ac3_immo.py (gabarit partagé).

Interface : async def search(criteres: dict) -> list[dict]
"""
from scrapers._ac3_immo import search_ac3
from scrapers._base import standalone_main

BASE_URL = "https://www.apally.com"


async def search(criteres: dict) -> list[dict]:
    return await search_ac3(
        criteres,
        base_url=BASE_URL,
        source="alain_pally",
        label="AlainPally",
        agence="Alain Pally - Val du Loir Immobilier",
    )


if __name__ == "__main__":
    standalone_main(search, "Alain Pally - Val du Loir Immobilier")

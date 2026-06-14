"""scrapers/la_chaumiere.py — Agence La Chaumière (Maintenon, Eure-et-Loir, 28)

Méthode : scrape_simple (httpx) — SSR HTML, CMS AC3 / immo-facile.
Couverture : Maintenon, Épernon, Gallardon, Jouy, vallée de l'Eure (28).
Listing : /annonces/transaction/Vente.html (+ pagination _____{N}, ~8 pages).
Filtre département : CP lu en clair sur la carte (« VILLE (28xxx) ») puis
POST-FILTRE strict code_postal[:2] ∈ départements cibles → 0 fuite.

Logique factorisée dans scrapers/_ac3_immo.py (gabarit partagé).

Interface : async def search(criteres: dict) -> list[dict]
"""
from scrapers._ac3_immo import search_ac3
from scrapers._base import standalone_main

BASE_URL = "https://www.la-chaumiere.fr"


async def search(criteres: dict) -> list[dict]:
    return await search_ac3(
        criteres,
        base_url=BASE_URL,
        source="la_chaumiere",
        label="LaChaumiere",
        agence="Agence La Chaumière",
    )


if __name__ == "__main__":
    standalone_main(search, "Agence La Chaumière")

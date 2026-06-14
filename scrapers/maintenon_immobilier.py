"""scrapers/maintenon_immobilier.py — L'Immobilière de Maintenon (Eure-et-Loir, 28)

Méthode : scrape_simple (httpx) — SSR HTML, CMS AC3 / immo-facile.
Couverture : Maintenon, Épernon, Gallardon, Nogent-le-Roi, vallée de l'Eure (28).
Listing : /annonces/transaction/Vente.html (+ pagination _____{N}).
Filtre département : la liste n'est PAS filtrée par dept côté serveur → on lit le
CP en clair sur chaque carte (« VILLE (28xxx) ») et on POST-FILTRE strict
code_postal[:2] ∈ départements cibles → 0 fuite.

Logique factorisée dans scrapers/_ac3_immo.py (gabarit partagé avec la-chaumiere,
apally…).

Interface : async def search(criteres: dict) -> list[dict]
"""
from scrapers._ac3_immo import search_ac3
from scrapers._base import standalone_main

BASE_URL = "https://www.maintenonimmobilier.com"


async def search(criteres: dict) -> list[dict]:
    return await search_ac3(
        criteres,
        base_url=BASE_URL,
        source="maintenon_immobilier",
        label="MaintenonImmo",
        agence="L'Immobilière de Maintenon",
    )


if __name__ == "__main__":
    standalone_main(search, "L'Immobilière de Maintenon")

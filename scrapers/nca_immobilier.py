"""scrapers/nca_immobilier.py — NCA Immobilier (Indre-et-Loire, 37)

Méthode : scrape_simple (httpx) — SSR via le sitemap.xml du moteur Netty/Modelo.
Fiches sous /vente/{type}-...-{ville}-{CP},{REF} ; CP du slug → filtre dept 0 fuite.
Stock observé : ~97 biens résidentiels 37 (Tours / Touraine).

Voir scrapers/_netty.py pour le socle partagé.
Interface : async def search(criteres: dict) -> list[dict]
"""
from scrapers._netty import netty_search

BASE_URL = "https://www.nca-immobilier.fr"


async def search(criteres: dict) -> list[dict]:
    return await netty_search(
        criteres, BASE_URL, source="nca_immobilier",
        agence="NCA Immobilier", label="NCA",
    )


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "NCA Immobilier")

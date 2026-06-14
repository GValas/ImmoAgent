"""scrapers/terres_de_sologne.py — Terres de Sologne Immobilier (41/45)

Méthode : scrape_simple (httpx) — SSR via le sitemap.xml du moteur Netty/Modelo.
Fiches sous /vente/{type}-...-{ville}-{CP},{REF} ; CP du slug → filtre dept 0 fuite.
Profil rural Sologne (châteaux, propriétés). Stock observé : ~12 biens 41 + 2 en 45.

Voir scrapers/_netty.py pour le socle partagé.
Interface : async def search(criteres: dict) -> list[dict]
"""
from scrapers._netty import netty_search

BASE_URL = "https://www.terres-de-sologne.com"


async def search(criteres: dict) -> list[dict]:
    return await netty_search(
        criteres, BASE_URL, source="terres_de_sologne",
        agence="Terres de Sologne Immobilier", label="TerresSologne",
    )


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Terres de Sologne Immobilier")

"""scrapers/agencelorzimmobilier.py — Agence Lorz Immobilier (Bourges, 18)

Méthode : scrape_simple (httpx) — SSR via le sitemap.xml du moteur Netty/Modelo.
Fiches sous /vente/{type}-...-{ville}-{CP},{REF} ; CP du slug → filtre dept 0 fuite.
Stock observé : ~95 biens résidentiels 18 (Cher).

Voir scrapers/_netty.py pour le socle partagé.
Interface : async def search(criteres: dict) -> list[dict]
"""
from scrapers._netty import netty_search

BASE_URL = "https://www.agencelorzimmobilier.com"


async def search(criteres: dict) -> list[dict]:
    return await netty_search(
        criteres, BASE_URL, source="agencelorzimmobilier",
        agence="Agence Lorz Immobilier", label="Lorz",
    )


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Agence Lorz Immobilier")

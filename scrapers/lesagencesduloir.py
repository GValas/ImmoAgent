"""scrapers/lesagencesduloir.py — Les Agences du Loir (Sarthe/Maine-et-Loire)

Méthode : scrape_simple (httpx) — SSR via le sitemap.xml du moteur Netty/Modelo.
Fiches sous /vente/{type}-...-{ville}-{CP},{REF} ; CP du slug → filtre dept 0 fuite.
Stock observé : ~63 biens 72 + ~38 biens 49.

Voir scrapers/_netty.py pour le socle partagé.
Interface : async def search(criteres: dict) -> list[dict]
"""
from scrapers._netty import netty_search

BASE_URL = "https://www.lesagencesduloir.com"


async def search(criteres: dict) -> list[dict]:
    return await netty_search(
        criteres, BASE_URL, source="lesagencesduloir",
        agence="Les Agences du Loir", label="AgencesDuLoir",
    )


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Les Agences du Loir")

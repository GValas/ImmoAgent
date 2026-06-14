"""scrapers/topaze_immobilier.py — Topaze Immobilier (Tours, 37)

Méthode : scrape_simple (httpx) — SSR via le sitemap.xml du moteur Netty/Modelo.
Les pages /vente et fiches sont rendues React côté client (vides en httpx) mais le
sitemap liste toutes les fiches sous /vente/{type}-...-{ville}-{CP},{REF}. Le CP du
slug donne le département (filtre 0 fuite via CP[:2]). Stock observé : ~84 biens 37.

Voir scrapers/_netty.py pour le détail du socle partagé.
Interface : async def search(criteres: dict) -> list[dict]
"""
from scrapers._netty import netty_search

BASE_URL = "https://www.topaze-immobilier.com"


async def search(criteres: dict) -> list[dict]:
    return await netty_search(
        criteres, BASE_URL, source="topaze_immobilier",
        agence="Topaze Immobilier", label="Topaze",
    )


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Topaze Immobilier")

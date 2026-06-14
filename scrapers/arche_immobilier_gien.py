"""scrapers/arche_immobilier_gien.py — Arche Immobilier (Gien, 45)

Méthode : scrape_simple (httpx) — SSR via le sitemap.xml du moteur Netty/Modelo.
Fiches sous /vente/{type}-...-{ville}-{CP},{REF} ; CP du slug → filtre dept 0 fuite.
Stock observé : ~93 biens 45 (Loiret, secteur Gien) + 1 en 18.

Voir scrapers/_netty.py pour le socle partagé.
Interface : async def search(criteres: dict) -> list[dict]
"""
from scrapers._netty import netty_search

BASE_URL = "https://www.arche-immobilier-gien.com"


async def search(criteres: dict) -> list[dict]:
    return await netty_search(
        criteres, BASE_URL, source="arche_immobilier_gien",
        agence="Arche Immobilier Gien", label="ArcheGien",
    )


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Arche Immobilier Gien")

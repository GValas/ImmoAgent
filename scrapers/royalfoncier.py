"""scrapers/royalfoncier.py — Royal Foncier (Sainte-Maure-de-Touraine, 37)

Méthode : scrape_simple (httpx) — SSR via le sitemap.xml du moteur Netty/Modelo.
Profil IDÉAL : moulins, manoirs, longères, demeures de caractère en Touraine.
Les fiches sont sous /vente/{type}-...-{ville}-{CP},{REF} ; CP du slug → filtre dept
0 fuite (le site déborde sur le 86/Vienne, écarté par le post-filtre CP[:2]).
Stock observé en zone : ~71 biens 37 + 2 en 36 (sur ~112 hors-zone 86 écartés).

Voir scrapers/_netty.py pour le socle partagé.
Interface : async def search(criteres: dict) -> list[dict]
"""
from scrapers._netty import netty_search

BASE_URL = "https://www.royalfoncier.fr"


async def search(criteres: dict) -> list[dict]:
    return await netty_search(
        criteres, BASE_URL, source="royalfoncier",
        agence="Royal Foncier", label="RoyalFoncier",
    )


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, "Royal Foncier")

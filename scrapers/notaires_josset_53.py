"""scrapers/notaires_josset_53.py — Étude de Me Fabien JOSSET, Château-Gontier (53).

Méthode : scrape_simple (httpx) — SSR HTML (gabarit immobilier.notaires.fr / Genapi).
Site : https://josset-chateau-gontier.notaires.fr
URL : /annonces-immobilieres.html  (page liste unique, petit office, Mayenne 53).
Cartes : div.bloc-annonce — ville+dept dans .titre « CHATEAU GONTIER (53) »,
  type/pièces/surface/prix dans .titre-detail, lien détail vers immobilier.notaires.fr
  (le code dept y figure aussi). Parsing factorisé dans scrapers/_notaires_genapi.py.
Filtre DÉPARTEMENT : code dept des parenthèses de .titre, re-confirmé par l'URL détail,
  POST-FILTRE STRICT zone cible → 0 fuite. CP exact récupéré en page détail (gallery.py).

Interface : async def search(criteres: dict) -> list[dict]
"""
from __future__ import annotations

from scrapers._notaires_genapi import run_office_search

BASE_URL = "https://josset-chateau-gontier.notaires.fr"
SOURCE = "notaires_josset_53"
LABEL = "NotairesJosset53"
AGENCE = "Étude de Me Fabien Josset (Château-Gontier)"


async def search(criteres: dict) -> list[dict]:
    return await run_office_search(
        base_url=BASE_URL, source=SOURCE, label=LABEL, agence=AGENCE,
        criteres=criteres,
    )


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, LABEL)

"""scrapers/notaires_garban_lafleche_72.py — Office notarial GARBAN, HERVE & BOUTET,
La Flèche (72).

Méthode : scrape_simple (httpx) — SSR HTML (gabarit immobilier.notaires.fr / Genapi,
div.bloc-annonce). Parsing factorisé dans scrapers/_notaires_genapi.py.
Site : https://garban-herve-boutet-lafleche.notaires.fr
URL : /annonces-immobilieres.html  (page liste unique, office Sud-Sarthe, stock en 72).
Filtre DÉPARTEMENT : code dept extrait des parenthèses de .titre « LA FLECHE (72) » et
  re-confirmé par l'URL détail immobilier.notaires.fr ; POST-FILTRE STRICT zone cible →
  0 fuite. CP exact récupéré en page détail (gallery.py).

Interface : async def search(criteres: dict) -> list[dict]
"""
from __future__ import annotations

from scrapers._notaires_genapi import run_office_search

BASE_URL = "https://garban-herve-boutet-lafleche.notaires.fr"
SOURCE = "notaires_garban_lafleche_72"
LABEL = "NotairesGarbanLaFleche72"
AGENCE = "Office notarial Garban, Hervé & Boutet (La Flèche)"


async def search(criteres: dict) -> list[dict]:
    return await run_office_search(
        base_url=BASE_URL, source=SOURCE, label=LABEL, agence=AGENCE,
        criteres=criteres,
    )


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, LABEL)

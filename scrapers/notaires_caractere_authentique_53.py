"""scrapers/notaires_caractere_authentique_53.py — Office Notarial Caractère
Authentique, Château-Gontier-sur-Mayenne (Mes MATHIEU & MASSERON, 53).

Méthode : scrape_simple (httpx) — SSR HTML (gabarit immobilier.notaires.fr / Genapi,
contenu présent dans le HTML brut, pas de JS).
Site : https://caractere-authentique-chateau-gontier.notaires.fr
URL pattern : /annonces-immobilieres.html  (page liste unique, petit office — pas de
              pagination réelle). PAS de filtre département serveur (cet office vend
              quasi-exclusivement en Mayenne 53).
Cartes : div.bloc-annonce
  - .titre        → « CHATEAU GONTIER (53) »  (ville + code département entre parenthèses)
  - .titre-type   → « Vente »
  - .titre-detail → « Maison / villa - 5 pièce(s) - 104 m² 178 100 € … »
  - lien détail   → a[href contient immobilier.notaires.fr/.../{ville-slug}-{dept}/{id}]
                    (le code département y figure aussi → double contrôle)
  - description   → .desc-immo-detail
  - photo         → img[data-src] (media.immobilier.notaires.fr)
Filtre DÉPARTEMENT : code dept extrait des parenthèses de .titre (et re-confirmé par
  l'URL détail) → POST-FILTRE STRICT sur la zone cible → 0 fuite hors-zone.
Particularité : le code postal EXACT n'est pas dans la carte (récupéré en page détail
  par gallery.py / geolocate.py) ; on renseigne departement + ville.

Interface : async def search(criteres: dict) -> list[dict]
"""
from __future__ import annotations

from scrapers._notaires_genapi import run_office_search

BASE_URL = "https://caractere-authentique-chateau-gontier.notaires.fr"
SOURCE = "notaires_caractere_authentique_53"
LABEL = "NotairesCaractereAuthentique53"
AGENCE = "Office Notarial Caractère Authentique (Château-Gontier)"


async def search(criteres: dict) -> list[dict]:
    return await run_office_search(
        base_url=BASE_URL, source=SOURCE, label=LABEL, agence=AGENCE,
        criteres=criteres,
    )


if __name__ == "__main__":
    from scrapers._base import standalone_main
    standalone_main(search, LABEL)

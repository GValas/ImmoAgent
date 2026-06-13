"""core — utilitaires partagés entre workers, orchestrator et scheduler.

Regroupe la logique autrefois dupliquée :
  - dedup        : clé de déduplication unique
  - filters      : filtres a posteriori (structurel + mots-clés + terrain + photos)
  - excel_export : écriture unique des classeurs Excel
  - dept_data    : noms de départements
  - logging_setup: horodatage centralisé des prints
"""

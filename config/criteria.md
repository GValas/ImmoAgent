# Critères de recherche immobilière
#
# SOURCE UNIQUE DE VÉRITÉ — tout ce que tu configures est ici.
#
# ─────────────────────────────────────────────────────────────────────────────
# Deux familles de critères :
#   1. SCRAPING  → filtre dur sur des champs STRUCTURÉS (type, surface, prix,
#      pièces, terrain, DPE). Aucune interprétation : un nombre, on garde ou on jette.
#   2. ANALYSE   → la description qualitative, matchée à l'annonce par NLP (tri).
#                  La géo (gare + liens satellite/cadastre) est automatique.
#
# Lecture (config_loader.py) : chaque bloc de code (entre triple accents graves)
# est un fragment YAML (clé: valeur, listes et chaînes multi-lignes, commentaires #) ;
# tous les blocs sont fusionnés. Les départements sont la clé "departements: [..]".
# ─────────────────────────────────────────────────────────────────────────────


# ── PHASE 1 — CRITÈRES DE SCRAPING (filtre dur, sans interprétation) ──────────
#
# `departements`, `types`, `surface_*`, `pieces_*`, `terrain_min`, `prix_*` et
# `dpe_exclus` sont envoyés aux scrapers quand le site le permet, PUIS re-vérifiés
# après scraping (hunter.filter_biens). Un bien sans prix/surface/DPE renseigné
# n'est PAS exclu par ces tests.

## Critères du bien

```
departements: [
    72,  # Sarthe
    28,  # Eure-et-Loir
    45,  # Loiret
    89,  # Yonne
    49,  # Maine-et-Loire
    37,  # Indre-et-Loire
    36,  # Indre
    18,  # Cher
    58,  # Nièvre
    41,  # Loir-et-Cher
    53,  # Mayenne
]
types:       ["maison", "propriete", "manoir", "longere"]
surface_min: 150        # m² habitables minimum
surface_max: 300
pieces_min:  6          # 4 chambres + séjour + cuisine minimum
pieces_max:  13         # au-delà = trop grand / immeuble de rapport
terrain_min: 4000       # m² — terrain arboré impératif
prix_min:    300000     # €
prix_max:    600000     # €
dpe_exclus:  ["F", "G"] # A B C D E acceptés
photos_min:  3          # exclure les annonces avec moins de N photos
```


# ── FILTRE MOTS-CLÉS (filtre dur sur le TEXTE de l'annonce) ───────────────────
#
# Deux listes ÉLIMINATOIRES, appliquées au titre + description COMPLÈTE de
# l'annonce. Contrairement aux critères structurés ci-dessus (qui filtrent au
# requêtage du site), ce filtre s'applique APRÈS le 1er filtrage, sur l'annonce
# entière une fois sa page détail récupérée (description complète) — tout comme
# la description qualitative (Phase 2).
# Insensible à la casse et aux accents ; correspondance par sous-chaîne (donc
# « pierre » matche aussi « pierres »). À distinguer de la description qualitative,
# qui n'élimine pas mais trie.
#   • mots_obligatoires : TOUS doivent être présents (logique ET) — un seul
#     manquant ⇒ le bien est écarté.
#   • mots_interdits    : si un seul est présent ⇒ le bien est écarté.
# Laisser une liste vide ([]) la désactive.

## Filtre mots-clés

```
mots_obligatoires: ["piscine"]        
mots_interdits:    ["viager", "hors-sol", "hors sol"]      
```


# ── PHASE 2 — DESCRIPTION QUALITATIVE (NLP, non éliminatoire) ─────────────────
#
# Texte libre comparé SÉMANTIQUEMENT (embeddings) au titre + description de chaque
# annonce. Remplit les colonnes Excel « Match qual. » (0–100) et « Extrait qual. »,
# et TRIE les résultats. Synonymes/reformulations compris (« cachet » ≈ « caractère »).
# Désactiver : laisser le texte vide. NB : les similarités se tassent vers 30–60,
# c'est l'ORDRE relatif qui compte.

## Description qualitative

```
description_qualitative: |
  - A moins de 4h de Paris 11e en train sans voiture
  - Proche de tous commerces (boulangerie, médecin, supermarché)
  - Zone non inondable ; idéalement proche d'un fleuve ou d'une rivière
  - Piscine : au moins 4×9 m — éliminatoire si absente
  - Bon état général, sans travaux à prévoir
  - Champêtre, caractère, authentique
  - Matériaux : pierres apparentes, pierre de taille, colombages, briques anciennes
  - PAS de contemporain / lotissement / moderne
```


# ── ANALYSE GÉO (automatique — rien à configurer) ────────────────────────────
#
# Après scraping, chaque bien est annoté (toujours actif, non éliminatoire) :
#   • gare voyageurs la plus proche (nom + distance) → colonne Excel « Gare » ;
#   • liens de vérification : satellite Google + ortho/cadastre Geoportail.


# ── SCHEDULER (pipeline continu) ─────────────────────────────────────────────

## Scheduler

```
hunter_interval_hours:   1     # fréquence de scraping (heures)
discovery_interval_days: 1
builder_interval_days:   30
max_biens_suivi:         100
```

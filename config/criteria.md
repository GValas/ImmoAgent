# Critères de recherche immobilière
#
# SOURCE UNIQUE DE VÉRITÉ — tout ce que tu configures est ici.
#
# ─────────────────────────────────────────────────────────────────────────────
# Deux phases dans le pipeline, deux familles de critères :
#
#   1. CRITÈRES DE SCRAPING  → filtrage sur les caractéristiques STRUCTURÉES
#      (type, surface, prix, pièces, terrain, DPE). Soit envoyés dans la requête
#      au site, soit appliqués juste après sur les champs déjà parsés. Aucune
#      interprétation : un nombre, une catégorie, on garde ou on jette.
#
#   2. CRITÈRES D'ANALYSE    → demandent d'INTERPRÉTER l'annonce :
#        • le TEXTE   (mots-clés rédhibitoires, équipements mentionnés)
#        • le VISUEL  (CLIP sur les photos : éléments indésirables)
#        • la GÉO     (gare/bus à proximité, cadastre, satellite)
#      + la PONDÉRATION du score final.
#
# ─────────────────────────────────────────────────────────────────────────────
# Comment ce fichier est lu (config_loader.py) :
#   • le 1ᵉʳ bloc de code (entre triple accents graves) = liste des départements ;
#   • dans TOUS les blocs de code, chaque ligne "clé: valeur" devient un réglage.
#   Donc : ne pas déplacer le bloc Départements de la 1ʳᵉ place, et ne mettre que
#   des "clé: valeur" (ou des commentaires #) dans les blocs de config.
# ─────────────────────────────────────────────────────────────────────────────


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  ZONE DE RECHERCHE                                                         ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

## Départements

# Codes INSEE des départements ciblés (doit rester le 1ᵉʳ bloc du fichier).

```
72  # Sarthe
28  # Eure-et-Loir
45  # Loiret
89  # Yonne
49  # Maine-et-Loire
37  # Indre-et-Loire
36  # Indre
18  # Cher
58  # Nièvre
41  # Loir-et-Cher
53  # Mayenne
```


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  PHASE 1 — CRITÈRES DE SCRAPING (caractéristiques structurées)             ║
# ║  Filtrent sur des champs précis de l'annonce, sans interprétation.         ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

## Critères du bien

# `types`, `surface_*`, `pieces_min`, `terrain_min`, `prix_*` sont passés en
# paramètres de requête aux scrapers quand le site le permet. Les bornes
# `surface_min`, `prix_min/max`, `pieces_max` et `dpe_exclus` sont en plus
# re-vérifiées après scraping (filtre dur dans hunter.filter_biens).
# NB : un bien sans prix / surface / DPE renseigné n'est PAS exclu par ces tests.

```
types:        ["maison", "propriete", "manoir", "longere"]
surface_min:  150       # m² habitables minimum
surface_max:  300
pieces_min:   6         # 4 chambres + séjour + cuisine minimum
pieces_max:   13        # au-delà = trop grand / immeuble de rapport
terrain_min:  4000      # m² — terrain arboré impératif
prix_min:     300000    # €
prix_max:     600000    # €
dpe_exclus:   ["E", "F", "G"]    # A B C D acceptés
photos_min:   3         # exclure les annonces avec moins de N photos (info trop pauvre)
```


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  PHASE 2 — CRITÈRES D'ANALYSE (interprétation de l'annonce)                ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

## Analyse du TEXTE (description + titre)

# Lecture du texte de l'annonce (pas un champ structuré).
#   mots_cles_negatifs : si l'un apparaît dans titre/description → bien exclu.
#   equipements_requis : exigés ; pour « piscine », validé par le texte
#                        (mention propre au bien) OU par la détection visuelle CLIP.

```
mots_cles_negatifs: ["viager", "enchères", "occupé", "indivision", "inondable", "zone inondable", "toit plat", "toit-terrasse", "toit terrasse", "toiture terrasse", "toiture-terrasse", "toiture plate"]
equipements_requis: ["piscine"]
```

# Note « toit plat » : exclusion par TEXTE (CLIP confond toit plat et toit en pente,
# vérifié). Attrape les annonces qui le mentionnent ; une maison à toit plat dont le
# texte ne le dit pas passera (rare — c'est en général un argument mis en avant).


## Analyse VISUELLE (CLIP sur les photos) — Exclusions

# Filtrage par EXCLUSION : on décrit en français des éléments indésirables ;
# si CLIP en repère un dans une photo du bien, le bien est écarté (`exclusion`)
# ou simplement annoté (`alerte`). Pas de scoring de style positif.
# Format : `description française | mode`   (mode = exclusion | alerte)

```
piscine hors-sol (bassin posé au sol, à parois/habillage bois) | alerte
# gazon artificiel / pelouse trop parfaite                      | désactivé (non fiable en CLIP, cf. note)
# aucun arbre sur le terrain                                    | désactivé (non fiable en CLIP, cf. note)
```

> 🌳 *« aucun arbre »* et *« gazon parfait »* sont **désactivés** : CLIP ne sait pas
> détecter de façon fiable une *absence* d'arbres (déclenche sur vues de champs/cartes/plans)
> ni une *pelouse trop parfaite* (indistinguable d'une belle pelouse verte de maison de
> caractère). À juger à l'œil via les photos / le lien satellite de l'Excel.

> ⚙️ **Comment ça s'applique** : cette liste est ta source en français. Les prompts
> réellement soumis à CLIP vivent dans `config/elements.yaml` (en anglais — CLIP est
> entraîné en anglais, le français inverse la détection). Après avoir édité la liste
> ci-dessus, demande à **Claude Code** : « *synchronise les exclusions visuelles* ».
> Il traduit chaque ligne, choisit les prompts négatifs (confondants) et le seuil,
> puis met à jour `elements.yaml`. Un élément non calibré reste en `alerte`
> (non destructif) jusqu'à validation (`python agents/vision.py --calibrer <nom>`).


## Analyse GÉO — Gare & bus (proximité)

# Calculée après scraping : distance entre le bien (coords ou centre-commune
# géocodé) et la gare voyageurs / l'arrêt de bus le plus proche.
#   gare_obligatoire : si true, exclut les biens sans gare dans le rayon.
#   bus_*            : informatif seulement (jamais éliminatoire).

```
gare_obligatoire: true     # éliminer les biens sans gare SNCF voyageurs à proximité
gare_rayon_km:    15       # rayon max (km) bien ↔ gare la plus proche
bus_actif:        true     # annoter l'arrêt de bus le plus proche (NON éliminatoire)
bus_rayon_km:     2        # rayon court (km) — un arrêt utile est proche du bien
```


## Analyse GÉO — Géolocalisation (cadastre, satellite, piscine ortho)

# Pré-localise le bien (coords approx. de l'annonce × cadastre IGN) et ajoute à
# l'Excel : lien satellite Google, lien ortho+cadastre Geoportail, parcelle
# probable, et — si geoloc_piscine_ortho — détection piscine sur l'orthophoto IGN.

```
geoloc_actif:           true   # pré-localisation cadastrale (liens satellite + parcelles)
geoloc_piscine_ortho:   true   # détecter la piscine sur l'orthophoto IGN (plus lent)
geoloc_terrain_tol_pct: 25     # écart toléré contenance cadastrale vs terrain annoncé (%)
```


## Critères qualitatifs (libres — lus par Claude Code, hors pipeline auto)

# Non parsés : repères pour l'analyse manuelle / les questions à Claude Code.
#
# Accessibilité
#   - Moins de 4h de Paris 11e en train sans voiture
#   - Proche de tous commerces (boulangerie, médecin, supermarché)
#   - Zone non inondable ; idéalement proche d'un fleuve ou d'une rivière
# Équipement impératif
#   - Piscine : au moins 4×9 m — éliminatoire si absente
# Confort souhaité
#   - Bon état général, sans travaux à prévoir


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  SCORING — Pondération du classement final                                 ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

## Pondérations du scoring

# Poids relatifs des dimensions du score (0–100). Renormalisés automatiquement
# si la somme ≠ 100. (Le style visuel n'entre plus dans le score.)

```
poids_prix:          5    # position dans la fourchette de prix
poids_surface:       15   # >= surface_min
poids_terrain:       10   # >= terrain_min — critère fort
poids_localisation:  35   # gare/commerces/accès Paris
poids_etat:          10   # bon état, sans travaux
poids_dpe:           5    # A/B/C/D
```


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  SCHEDULER (pipeline continu) — laissé tel quel                            ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

## Scheduler

```
hunter_interval_hours:    1     # scrape toutes les 6h
discovery_interval_days:  1
builder_interval_days:    30
score_seuil_interet:      50
max_biens_suivi:          100
```

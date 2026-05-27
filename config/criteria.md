# Critères de recherche immobilière
# Ce fichier est la source unique de vérité — tout ce que tu modifies est ici.
# Critères classés par importance décroissante.

---

## Départements

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

---

## Critères du bien

```
types:        ["maison", "propriete", "manoir", "longere"]
surface_min:  150       # m² habitables minimum
surface_max:  300
pieces_min:   6         # 4 chambres + séjour + cuisine minimum
pieces_max:   13        # au-delà = trop grand / immeuble de rapport
terrain_min:  4000      # m² — terrain arboré impératif
prix_min:     330000    # €
prix_max:     550000    # €
dpe_exclus:   ["E", "F", "G"]    # A B C D acceptés
```

---

## Pondérations du scoring (total = 100)

```
poids_prix:          5   # dans la fourchette 350-550k
poids_surface:       15   # >= 150m²
poids_terrain:       10   # >= 4000m² arboré — critère fort
poids_localisation:  35   # accès train Paris < 4h, commerces proches
poids_etat:          10   # bon état, sans travaux
poids_dpe:            5   # A/B/C/D
poids_style:         10   # style ancien/rustique/campagnard (CLIP)
```

---

## Filtres d'exclusion

```
mots_cles_negatifs: ["viager", "enchères", "occupé", "indivision", "inondable", "zone inondable"]
equipements_requis: ["piscine"]
```

---

## Critères qualitatifs (utilisés par Claude Code pour l'analyse)

# Accessibilité 
# - Moins de 4h de Paris 11e en train sans voiture
# - Proche de tous commerces (boulangerie, médecin, supermarché)
# - En zone non inondable
# - Idéalement proche d'un fleuve ou d'une rivière

# Équipement impératif
# - Piscine : au moins 4x9m — critère éliminatoire si absente

# Confort souhaité
# - Bon état général, sans travaux à prévoir

---

## Style visuel

Style architectural recherché : ancien, rustique, campagnard, moderne


- **Architecture** : longère, maison de maître, manoir, ferme rénovée
- **Matériaux** : briques apparentes, pierre de taille, colombages, bois
- **Extérieur** : grand terrain arboré (4000m² min), piscine, calme, vue sur parc ou nature
- **Ambiance** : campagne française authentique, pas de lotissement, pas de style contemporain
- **Intérieur** : volumes généreux, cheminées, poutres, parquet ancien

### Styles à exclure

- Maison de lotissement (années 70–2000, plain-pied sans caractère)
- Architecture moderne (béton, toit plat, baies vitrées industrielles)
- Pavillon de banlieue ou périurbain

### Références visuelles
- Les photos de ce que tu VEUX sont dans config/style_references/
- Les photos de ce que tu NE VEUX PAS sont dans config/style_ban/

### Seuils de filtrage CLIP

```
style_seuil_exclusion: 30   # score style < X → rejeté
style_seuil_warning:   50   # score style < X → alerte
style_seuil_ban:       99   # score ban > X   → rejeté (ressemble trop à une image ban)
```

---

## Scheduler

```
hunter_interval_hours:    1     # scrape toutes les 6h
discovery_interval_days:  1
builder_interval_days:    30
score_seuil_interet:      50
max_biens_suivi:          50
```

# config/style_ban/ — Images de styles à exclure

Ce dossier contient les photos de référence des biens que tu **ne veux PAS**.

## Comment ça marche

L'agent Vision compare chaque photo d'annonce avec ces images via CLIP (ViT-B/32, local).  
Si la similarité cosinus dépasse `style_seuil_ban` (défaut : 70/100), le bien est **rejeté
automatiquement** — même si son score de style positif est bon.

## Quelles images mettre ici ?

Des exemples représentatifs des styles indésirables :

- **Pavillons de lotissement** (années 70–2000, plain-pied, tuiles mécaniques, jardin grillage)
- **Maisons contemporaines** (toit plat, béton brut, baies vitrées industrielles)
- **Constructions récentes sans caractère** (enduit crépi beige, volets PVC, garage intégré)
- **Immeubles de rapport** (multiple boîtes aux lettres, façade répétitive)
- **Constructions industrielles** (hangar, entrepôt, atelier)

## Formats acceptés

`.jpg`, `.jpeg`, `.png`, `.webp`  
Pas de limite en nombre — 5 à 20 images suffisent pour un bon filtrage.

## Réglage du seuil

Dans `config/criteria.md`, section `### Seuils de filtrage CLIP` :

```
style_seuil_ban: 70   # défaut — diminue pour rejeter plus, augmente pour rejeter moins
```

- **Trop de faux positifs** (beaux biens rejetés) → augmente le seuil (ex: 80)
- **Des pavillons passent encore** → diminue le seuil (ex: 60) ou ajoute plus d'images ban

## Notes

- Les embeddings ban sont calculés **une seule fois** au démarrage (cache en mémoire).
- Si le dossier est vide ou absent, le filtre ban est simplement **désactivé** sans erreur.
- Le champ `score_ban` (0–100) et `banni` (bool) sont ajoutés à chaque bien dans le JSON brut.

# Répertoire de références visuelles

Place ici toutes tes photos de référence. Aucune limite de nombre.
Le modèle CLIP tourne entièrement en local — rien n'est envoyé à une API.

## Formats acceptés
`jpg`, `jpeg`, `png`, `webp`

## Comment ça marche

Au premier lancement, CLIP encode toutes tes photos en vecteurs (fait une seule fois,
puis mis en cache pour la session). Pour chaque bien trouvé, ses photos sont encodées
et comparées par similarité cosinus à tes références. Le score final est la moyenne
des meilleures similarités.

## Conseils pour de bons résultats

**Variété** : mets des photos de façades, jardins, intérieurs, terrasses — CLIP
comprend tous les angles. Plus tes références sont variées, plus le scoring est robuste.

**Qualité > quantité pour les positifs** : 10 bonnes photos représentatives valent
mieux que 100 photos bruyantes.

**Pas de négatifs nécessaires** : CLIP mesure la proximité avec tes références,
pas l'éloignement. Tu n'as pas besoin de mettre des contre-exemples.

## Nommage (libre, aucune convention imposée)

```
mas_luberon_facade.jpg
bastide_piscine_var.jpg
villa_pierre_terrasse.jpg
interieur_poutres_tommettes.jpg
jardin_provencal.jpg
...
```

## Performance indicative

| Nb références | Temps d'encodage initial | RAM supplémentaire |
|---|---|---|
| 10 photos      | ~3s                      | négligeable        |
| 50 photos      | ~8s                      | ~50 MB             |
| 200 photos     | ~25s                     | ~200 MB            |

L'encodage est fait **une seule fois** par session de recherche.
Pour 100 biens avec 5 photos chacun : ~15–30s de scoring total (CPU).

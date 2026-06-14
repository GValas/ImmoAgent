# Immo-Agent 🏠

Système multi-workers de recherche immobilière automatisée. Il scrape ~295 sources
d'annonces françaises actives (337 déclarées dans `config/sources.yaml`), filtre les
biens sur des critères structurés (prix, surface,
terrain, pièces, DPE), applique un filtre mots-clés, enrichit (DVF, géoloc, gare/bus),
**évalue la correspondance qualitative via un LLM local (Ollama)** et produit un Excel de
suivi trié par pertinence.

> **Aucun appel API Anthropic dans le code.** La seule intelligence *runtime* est un LLM
> local servi par Ollama (conteneur dédié). Aucune clé API n'est requise.

## Architecture

```
orchestrator.py           ← point d'entrée (pipeline à la demande)
scheduler.py              ← pipeline continu (boucle ; maintient suivi_actif.xlsx)
config_loader.py          ← parse config/criteria.md (source unique de vérité)
├── workers/discovery.py  ← Worker 1 : charge sources.yaml, filtre par département
├── workers/builder.py    ← Worker 2 : vérifie les scrapers disponibles
├── workers/hunter.py     ← Worker 3 : scrape // , déduplique, filtre structurel +
│                            mots-clés, enrichit page détail, annote gare/bus/géo
├── workers/analyst.py    ← Worker 4 : enrichit DVF, match qualitatif LLM, export Excel
└── workers/qualitative.py ← util analyst : score description_qualitative ↔ annonce (Ollama)
```

## Installation (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

L'image/app est légère (CPU) : pas de torch ni de modèle embarqué. Le match qualitatif
appelle un LLM local **Ollama**. En local, pour activer cette étape, fais tourner un
Ollama joignable et exporte `OLLAMA_HOST` (sinon l'étape est simplement sautée, le reste
du pipeline tourne normalement) :

```bash
export OLLAMA_HOST=http://127.0.0.1:11434     # un Ollama avec le modèle qwen2.5:3b
```

## Configuration — `config/criteria.md`

**Tout** se règle dans ce fichier (source unique de vérité). Quatre familles de critères :

| Famille | Section | Effet |
|---|---|---|
| **Structurés** (filtre dur) | `## Critères du bien` | départements, types, surface, pièces, terrain, prix, DPE, photos |
| **Mots-clés** (filtre dur sur le TEXTE) | `## Filtre mots-clés` | `mots_obligatoires` (ET) et `mots_interdits` (exclusion) |
| **Qualitatif** (tri, non éliminatoire) | `## Description qualitative` | texte libre évalué par le LLM → score « Match qual. » + tri |
| **Pipeline** | `## Scheduler` | intervalles, `max_biens_suivi` |

Boucle de réglage rapide (sans re-scraper, ~1 min) après chaque édition de `criteria.md` :

```bash
OLLAMA_HOST=http://127.0.0.1:11434 python orchestrator.py --only-analyse
```

> Astuce : une **exclusion** dans la description qualitative doit commencer la ligne par
> `pas de…` / `sans…` / `non…` (sinon elle est lue comme un critère positif). Pour écarter
> **dur**, mets le terme dans `mots_interdits` plutôt que dans la description qualitative.

## Utilisation

```bash
python orchestrator.py                       # pipeline complet (discovery → build → hunt → analyse)
python orchestrator.py --skip-discovery --skip-build   # scrapers déjà générés
python orchestrator.py --only-analyse        # re-filtre + re-score le dernier raw, sans re-scraper

# Workers / scrapers en isolation (debug)
python workers/hunter.py
python scrapers/bienici.py
```

## Déploiement en production (Docker + GPU)

Le **scheduler** tourne en boucle continue et c'est lui — et lui seul — qui maintient
`data/output/suivi_actif.xlsx`. La stack est **multi-conteneurs** (`docker-compose.yml`) :

- **`ollama`** — serveur LLM local (utilise le GPU) ;
- **`ollama-pull`** — tâche one-shot : télécharge le modèle au 1er lancement ;
- **`scheduler`** — le pipeline, parle à Ollama via le réseau Docker interne.

> ⚠️ `orchestrator.py` produit des instantanés `resultats_*.xlsx` mais **ne met jamais à
> jour** `suivi_actif.xlsx`. Seul le scheduler le fait.

### Prérequis sur l'hôte

- Docker Engine + Docker Compose v2
- **GPU NVIDIA** : driver à jour + [`nvidia-container-toolkit`](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- Sous **WSL2** : driver NVIDIA installé côté **Windows**, puis le toolkit dans WSL

Vérifier que Docker voit le GPU : `docker run --rm --gpus all ubuntu nvidia-smi`

### Build & lancement

```bash
./run_prod.sh                      # build + lance toute la stack (recommandé)
./run_prod.sh --logs               # + suit les logs du scheduler
./run_prod.sh --cpu                # Ollama sans GPU (plus lent)
./run_prod.sh --model qwen2.5:7b   # surcharge le modèle qualitatif
# ou directement :
docker compose up -d --build
docker compose logs -f scheduler   # logs du pipeline
docker compose logs -f ollama      # logs du LLM
```

Le GPU est utilisé par **Ollama** (l'app est CPU). Au 1er lancement, `ollama-pull`
télécharge le modèle (`qwen2.5:3b` par défaut, ~2 Go, persisté dans le volume
`ollama-models`) ; le scheduler **attend** qu'il soit prêt. Surcharge :
`QUALITATIVE_MODEL=qwen2.5:7b ./run_prod.sh`.

> Lancer les conteneurs « à la main » avec `docker run` exigerait de recréer le réseau et
> d'ordonner les dépendances : utilise `./run_prod.sh` ou `docker compose`, pas `docker run` brut.

### Persistance

| Montage | Contenu |
|---|---|
| `./data` → `/app/data` | `suivi_actif.xlsx`, `biens_vus.json`, `scheduler_state.json`, `raw/`, `output/` |
| `./config` → `/app/config` | `criteria.md` (édition à chaud : relu au cycle suivant) |
| `./logs` → `/app/logs` | journaux |
| volume `ollama-models` | modèles Ollama (pas de re-téléchargement au redémarrage) |

### Exploitation

```bash
docker compose ps
docker compose logs -f scheduler
docker compose restart scheduler   # relit criteria.md
docker compose down
```

### ⚠️ Un seul scheduler à la fois

Ne pas lancer `scheduler.py` sur l'hôte **et** dans le conteneur simultanément : ils
écriraient dans le même `data/` → conflits.

## Output

- `data/raw/biens_raw_YYYYMMDD_HHMM.json` — biens bruts survivants (sortie du Hunter)
- `data/output/resultats_YYYYMMDD_HHMM.xlsx` — Excel final, trié par match qualitatif
- `data/output/suivi_actif.xlsx` — suivi cumulatif (maintenu par le scheduler)

### Colonnes Excel (principales)

| Colonne | Description |
|---|---|
| Match qual. | Score LLM 0–100 de correspondance avec `description_qualitative` (tri) |
| Extrait qual. | Justification courte du LLM |
| Prix / Prix m² / Prix m² marché | prix du bien et médiane DVF du département |
| DPE, Gare, Bus | annotations (non éliminatoires) |
| Satellite / Ortho+cadastre | liens de vérification visuelle (Google / Geoportail) |
| Alertes | anomalies (DPE, prix vs marché, doublon photo fusionné…) |

## Ajouter un scraper

Crée `scrapers/mon_site.py` exposant l'interface obligatoire :

```python
async def search(criteres: dict) -> list[dict]:
    """criteres: {departements, types_bien, surface_min, prix_max, ...}
    Retourne une liste de dicts conformes au modèle Bien (models.py)."""
    ...
```

Puis déclare la source dans `config/sources.yaml`. Voir `CLAUDE.md` pour les conventions,
la blacklist anti-bot et la liste des scrapers actifs.

## Notes légales

- **DVF (data.gouv.fr)**, **immonot**, **immobilier.notaires.fr** : usage autorisé
- **LeBonCoin**, **SeLoger**, **PAP**, **Logic-Immo** : protégés (Cloudflare / CGU) — voir la
  blacklist dans `sources.yaml`
- On ne scrape pas les tuiles cartographiques (liens pour clic humain uniquement)
- Pour usage intensif sur sites bloqués : Apify / ScraperAPI

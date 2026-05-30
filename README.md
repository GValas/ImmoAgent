# Immo-Agent 🏠

Système multi-agents Claude pour la recherche automatisée de biens immobiliers.

## Architecture

```
orchestrator.py          ← point d'entrée unique
├── agents/discovery.py  ← Agent 1 : identifie les sources via web_search
├── agents/builder.py    ← Agent 2 : génère les scrapers Python
├── agents/hunter.py     ← Agent 3 : lance les scrapers en parallèle
└── agents/analyst.py    ← Agent 4 : score, enrichit DVF, export Excel
```

## Installation

```bash
git clone / copier le projet
cd immo-agent

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # si scraping JS nécessaire

export ANTHROPIC_API_KEY=sk-ant-...
```

## Configuration

### 1. Définir tes départements cibles

Édite `config/criteria.md` — bloc de code du haut :
```
06  # Alpes-Maritimes
83  # Var
```

### 2. Ajuster les critères

Édite `config/criteres.yaml` : budget, surface, types de biens, pondérations de scoring.

## Utilisation

```bash
# Pipeline complet (discovery → build → hunt → analyse)
python orchestrator.py

# Re-lancer seulement la recherche (scrapers déjà générés)
python orchestrator.py --skip-discovery --skip-build

# Re-scorer le dernier jeu de données sans re-scraper
python orchestrator.py --only-analyse

# Agents individuels
python agents/discovery.py
python agents/builder.py
python agents/hunter.py
python agents/analyst.py
```

## Déploiement en production (Docker + GPU)

Le **scheduler** (`scheduler.py`) tourne en boucle continue et c'est lui — et lui seul —
qui maintient `data/output/suivi_actif.xlsx` (le fichier de suivi cumulatif). En prod on
le fait tourner dans un conteneur Docker avec GPU NVIDIA pour le scoring CLIP.

> ⚠️ `orchestrator.py` produit des instantanés `resultats_*.xlsx` mais **ne met jamais à
> jour** `suivi_actif.xlsx`. Seul le scheduler le fait.

### Prérequis sur l'hôte

- Docker Engine + Docker Compose v2
- **GPU NVIDIA** : driver à jour + [`nvidia-container-toolkit`](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- Sous **WSL2** : driver NVIDIA installé côté **Windows** (pas dans WSL), puis le toolkit dans WSL

Vérifier que Docker voit le GPU :
```bash
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

### Build & lancement

```bash
docker compose up -d --build       # construit l'image (torch CUDA + Chromium + CLIP) et démarre le scheduler
docker compose logs -f scheduler   # suivre la boucle en direct
```

Au démarrage, le log doit afficher le GPU — sinon le conteneur **s'arrête en erreur**
(garde-fou `IMMO_FORCE_GPU=1`, pas de repli CPU silencieux) :
```
[Vision] CLIP chargé sur cuda (NVIDIA GeForce RTX ...)
```

### Persistance

Trois volumes sont montés (cf. `docker-compose.yml`) :

| Montage | Contenu | Pourquoi |
|---|---|---|
| `./data` → `/app/data` | `suivi_actif.xlsx`, `biens_vus.json`, `scheduler_state.json`, `raw/`, `output/` | survit aux redémarrages **et** reste lisible depuis l'hôte (ouvre l'Excel dans `./data/output/`) |
| `./config` → `/app/config` | `criteria.md`, `style_references/` | édition **à chaud** : le prochain cycle relit `criteria.md`, pas de rebuild |
| `./logs` → `/app/logs` | journaux | accessibles depuis l'hôte |

### Exploitation

```bash
docker compose ps                  # état du conteneur
docker compose logs -f scheduler   # logs en direct
docker compose restart scheduler   # redémarrer (relit criteria.md)
docker compose down                # arrêter
docker compose up -d --build       # après un git pull / nouveau scraper : rebuild + relance
```

### Repli CPU (sans GPU)

Pour tourner sans GPU : dans `Dockerfile` remplacer l'index `cu124` par `cpu`, et dans
`docker-compose.yml` retirer `gpus: all` et passer `IMMO_FORCE_GPU` à `"0"`.

### ⚠️ Un seul scheduler à la fois

Ne pas lancer `scheduler.py` sur l'hôte **et** dans le conteneur simultanément : ils
écriraient dans le même `data/` (dédup, état, Excel) → conflits.

## Output

- `data/raw/biens_raw_YYYYMMDD_HHMM.json` — données brutes
- `data/output/resultats_YYYYMMDD_HHMM.xlsx` — Excel scoré avec résumé LLM

### Colonnes Excel

| Colonne | Description |
|---|---|
| Score | Score pondéré /100 (vert ≥70, jaune ≥45, rouge <45) |
| Prix/m² | Prix calculé du bien |
| Prix/m² marché | Médiane DVF du département |
| Alertes | Anomalies détectées (DPE, prix, travaux...) |

## Ajouter un scraper manuellement

Crée `scrapers/mon_site.py` avec cette interface :

```python
async def search(criteres: dict) -> list[dict]:
    """
    criteres: {departements, types_bien, surface_min, prix_max, ...}
    Retourne une liste de dicts conformes au modèle Bien.
    """
    ...
```

Puis ajoute la source dans `config/sources.yaml`.

## Notes légales

- **PAP**, **DVF (data.gouv.fr)**, **immonot** : usage autorisé
- **LeBonCoin**, **SeLoger** : scraping interdit par CGU — utiliser avec discernement
- Pour usage intensif, privilégier des services comme Apify ou ScraperAPI

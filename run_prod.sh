#!/usr/bin/env bash
# ============================================================
# run_prod.sh — construit et lance la stack immo-agent (multi-conteneurs)
# via docker compose : conteneur `ollama` (LLM local, GPU) + `scheduler`.
#
# Usage :
#   ./run_prod.sh                       # build + (re)lance la stack en arrière-plan
#   ./run_prod.sh --logs                # idem, puis suit les logs du scheduler
#   ./run_prod.sh --no-build            # relance sans reconstruire l'image
#   ./run_prod.sh --cpu                 # Ollama sans GPU (repli CPU, plus lent)
#   ./run_prod.sh --model qwen2.5:7b    # surcharge le modèle Ollama
#   ./run_prod.sh --clean               # `compose down` + purge des anciens
#                                       # conteneurs AVANT de relancer (repartir
#                                       # propre ; par défaut on fait juste `up`)
# ============================================================
set -euo pipefail

# Se placer à la racine du repo (où vit ce script), quelle que soit l'invocation.
cd "$(dirname "$(readlink -f "$0")")"

# --- Options ---
DO_BUILD=1
FOLLOW_LOGS=0
USE_GPU=1
DO_CLEAN=0
MODEL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-build) DO_BUILD=0 ;;
    --logs)     FOLLOW_LOGS=1 ;;
    --cpu)      USE_GPU=0 ;;
    --clean)    DO_CLEAN=1 ;;
    --model)    MODEL="${2:-}"; shift ;;
    *) echo "Option inconnue : $1" >&2; exit 2 ;;
  esac
  shift
done

# --- Pré-requis ---
command -v docker >/dev/null || { echo "❌ Docker n'est pas installé." >&2; exit 1; }
docker compose version >/dev/null 2>&1 || {
  echo "❌ 'docker compose' (plugin v2) indisponible." >&2; exit 1; }

# --- Préparer les répertoires montés (sinon Docker les crée en root) ---
mkdir -p data/raw data/output logs

# --- Fichiers compose : base (+ override CPU si demandé) ---
COMPOSE_FILES=(-f docker-compose.yml)
if [ "$USE_GPU" -eq 0 ]; then
  COMPOSE_FILES+=(-f docker-compose.cpu.yml)
  echo "==> Mode CPU : Ollama sans GPU (plus lent)"
else
  echo "==> Mode GPU : conteneur ollama avec --gpus all"
fi

# --- Modèle Ollama (surcharge optionnelle) ---
if [ -n "$MODEL" ]; then
  export QUALITATIVE_MODEL="$MODEL"
  echo "==> Modèle Ollama : $QUALITATIVE_MODEL"
fi

# --- CACHEBUST = timestamp → force la reconstruction de la couche code (COPY . .)
#     tout en gardant les couches lourdes (Playwright) en cache. ---
export CACHEBUST="$(date +%s)"

# --- Nettoyage OPTIONNEL (--clean) : `compose down` + retrait d'éventuels
#     conteneurs aux noms fixes laissés par un déploiement précédent (ancien
#     `docker run`, ou run compose interrompu) qui feraient échouer `compose up`
#     sur « container name already in use ». Les volumes (dont ollama-models) et
#     le réseau persistent. Par défaut on NE stoppe PAS la stack : `up -d`
#     recrée seulement ce qui a changé (pas d'interruption inutile). ---
if [ "$DO_CLEAN" -eq 1 ]; then
  echo "==> Nettoyage (--clean) : arrêt de la stack + purge des anciens conteneurs"
  docker compose "${COMPOSE_FILES[@]}" down --remove-orphans >/dev/null 2>&1 || true
  docker rm -f immo-agent-scheduler immo-agent-ollama immo-agent-ollama-pull >/dev/null 2>&1 || true
fi

# --- Build + lancement ---
UP_ARGS=(up -d --remove-orphans)
if [ "$DO_BUILD" -eq 1 ]; then
  echo "==> Build de l'image scheduler + pull de l'image ollama (peut prendre du temps)"
  UP_ARGS+=(--build)
else
  echo "==> Build sauté (--no-build)"
fi

docker compose "${COMPOSE_FILES[@]}" "${UP_ARGS[@]}"

echo
echo "✅ Stack démarrée."
docker compose "${COMPOSE_FILES[@]}" ps
echo
echo "Le service ollama-pull télécharge le modèle au 1er lancement (modèle persisté"
echo "ensuite dans le volume 'ollama-models'). Le scheduler attend qu'il soit prêt."
echo
echo "Logs scheduler : docker compose logs -f scheduler"
echo "Logs LLM       : docker compose logs -f ollama"
echo "Arrêt          : docker compose down"
echo "Suivi          : data/output/suivi_actif.xlsx"

# --- Suivre les logs si demandé ---
if [ "$FOLLOW_LOGS" -eq 1 ]; then
  echo "==> Suivi des logs scheduler (Ctrl-C pour quitter, les conteneurs continuent)"
  exec docker compose "${COMPOSE_FILES[@]}" logs -f scheduler
fi

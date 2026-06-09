#!/usr/bin/env bash
# ============================================================
# run_prod.sh — construit l'image et lance le scheduler immo-agent
# en conteneur de prod (GPU + volumes persistés).
#
# Usage :
#   ./run_prod.sh              # build + (re)lance le conteneur en arrière-plan
#   ./run_prod.sh --logs       # idem, puis suit les logs en direct
#   ./run_prod.sh --no-build   # relance sans reconstruire l'image
#   ./run_prod.sh --cpu        # sans GPU (repli CPU, pas de --gpus)
# ============================================================
set -euo pipefail

IMAGE="immo-agent:prod"
CONTAINER="immo-agent-scheduler"

# Se placer à la racine du repo (où vit ce script), quelle que soit l'invocation.
cd "$(dirname "$(readlink -f "$0")")"

# --- Options ---
DO_BUILD=1
FOLLOW_LOGS=0
USE_GPU=1
for arg in "$@"; do
  case "$arg" in
    --no-build) DO_BUILD=0 ;;
    --logs)     FOLLOW_LOGS=1 ;;
    --cpu)      USE_GPU=0 ;;
    *) echo "Option inconnue : $arg" >&2; exit 2 ;;
  esac
done

# --- Pré-requis ---
command -v docker >/dev/null || { echo "❌ Docker n'est pas installé." >&2; exit 1; }

# --- Préparer les répertoires montés (sinon Docker les crée en root) ---
mkdir -p data/raw data/output logs

# --- Build ---
if [ "$DO_BUILD" -eq 1 ]; then
  echo "==> Construction de l'image $IMAGE (torch + Chromium + modèle NLP, peut prendre plusieurs minutes)"
  # CACHEBUST = timestamp → force la reconstruction de la couche code (COPY . .) à
  # chaque build : le code est TOUJOURS frais, les couches lourdes restent cachées.
  docker build --build-arg CACHEBUST="$(date +%s)" -t "$IMAGE" .
else
  echo "==> Build sauté (--no-build)"
fi

# --- Remplacer un conteneur existant (idempotent) ---
if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "==> Suppression du conteneur existant $CONTAINER"
  docker rm -f "$CONTAINER" >/dev/null
fi

# --- Construire les options GPU ---
GPU_ARGS=()
if [ "$USE_GPU" -eq 1 ]; then
  GPU_ARGS=(--gpus all -e IMMO_FORCE_GPU=1)
  echo "==> Mode GPU (--gpus all, IMMO_FORCE_GPU=1)"
else
  GPU_ARGS=(-e IMMO_FORCE_GPU=0)
  echo "==> Mode CPU (--cpu) : pas de GPU exposé"
fi

# --- Lancer le scheduler (python scheduler.py = PID 1 du conteneur) ---
echo "==> Démarrage du conteneur $CONTAINER"
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  "${GPU_ARGS[@]}" \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/config:/app/config" \
  -v "$(pwd)/logs:/app/logs" \
  "$IMAGE" >/dev/null

# --- Vérifier qu'il tourne ---
sleep 2
if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ]; then
  echo "✅ $CONTAINER démarré."
  docker ps --filter "name=$CONTAINER" --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
  echo
  echo "Logs   : docker logs -f $CONTAINER"
  echo "Arrêt  : docker stop $CONTAINER"
  echo "Suivi  : data/output/suivi_actif.xlsx"
else
  echo "❌ $CONTAINER s'est arrêté immédiatement — dernières lignes du log :" >&2
  docker logs --tail 30 "$CONTAINER" >&2 || true
  exit 1
fi

# --- Suivre les logs si demandé ---
if [ "$FOLLOW_LOGS" -eq 1 ]; then
  echo "==> Suivi des logs (Ctrl-C pour quitter, le conteneur continue de tourner)"
  exec docker logs -f "$CONTAINER"
fi

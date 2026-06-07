#!/usr/bin/env bash
# ============================================================
# Stack Python + Playwright — SOURCE UNIQUE DE VÉRITÉ partagée par :
#   - Dockerfile           (image de prod, racine)
#   - .devcontainer/Dockerfile (devcontainer)
#
# À exécuter EN TANT QUE ROOT (playwright --with-deps installe des paquets apt)
# DEPUIS un répertoire contenant requirements.txt.
#
# Arg 1 (optionnel) : index des wheels torch. Défaut cu124 (GPU, prod + dev).
#   Repli CPU :  bash install-stack.sh https://download.pytorch.org/whl/cpu
# ============================================================
set -euo pipefail

TORCH_INDEX="${1:-https://download.pytorch.org/whl/cu124}"

# torch EN PREMIER : les wheels cu124 embarquent le runtime CUDA (cudart, cuDNN,
# cuBLAS) — pas besoin d'image de base CUDA, mais l'hôte doit fournir le driver
# NVIDIA + nvidia-container-toolkit et le conteneur recevoir `--gpus all`.
pip install torch torchvision --index-url "$TORCH_INDEX"
pip install -r requirements.txt
pip install git+https://github.com/openai/CLIP.git

# Playwright Chromium + libs système (apt).
playwright install --with-deps chromium

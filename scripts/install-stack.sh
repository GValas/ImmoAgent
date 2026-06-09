#!/usr/bin/env bash
# ============================================================
# Stack Python + Playwright — SOURCE UNIQUE DE VÉRITÉ partagée par :
#   - Dockerfile           (image de prod, racine)
#   - .devcontainer/Dockerfile (devcontainer)
#
# À exécuter EN TANT QUE ROOT (playwright --with-deps installe des paquets apt)
# DEPUIS un répertoire contenant requirements.txt.
#
# Arg 1 (optionnel) : index des wheels torch. Défaut cu126 (GPU, prod + dev).
#   cu126 est requis : transformers (tiré par sentence-transformers) utilise des
#   API torch récentes (ex. torch.float8_e8m0fnu) absentes des wheels cu124.
#   Repli CPU :  bash install-stack.sh https://download.pytorch.org/whl/cpu
# ============================================================
set -euo pipefail

TORCH_INDEX="${1:-https://download.pytorch.org/whl/cu126}"

# torch EN PREMIER : les wheels cu126 embarquent le runtime CUDA (cudart, cuDNN,
# cuBLAS) — pas besoin d'image de base CUDA, mais l'hôte doit fournir le driver
# NVIDIA + nvidia-container-toolkit et le conteneur recevoir `--gpus all`.
# torchvision n'est PAS installé : seul le modèle d'embeddings texte
# (sentence-transformers, requiert torch seul) l'utilisait via CLIP, désormais retiré.
pip install torch --index-url "$TORCH_INDEX"
pip install -r requirements.txt

# Playwright Chromium + libs système (apt).
playwright install --with-deps chromium

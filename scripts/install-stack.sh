#!/usr/bin/env bash
# ============================================================
# Stack Python + Playwright — SOURCE UNIQUE DE VÉRITÉ partagée par :
#   - Dockerfile           (image de prod, racine)
#   - .devcontainer/Dockerfile (devcontainer)
#
# À exécuter EN TANT QUE ROOT (playwright --with-deps installe des paquets apt)
# DEPUIS un répertoire contenant requirements.txt.
#
# NB : plus de torch ni sentence-transformers depuis le passage du matching
# qualitatif à un LLM local servi par Ollama (conteneur dédié, cf.
# docker-compose.yml). L'image applicative est redevenue 100 % CPU / légère ;
# c'est le conteneur `ollama` qui utilise le GPU.
# ============================================================
set -euo pipefail

pip install -r requirements.txt

# Playwright Chromium + libs système (apt).
playwright install --with-deps chromium

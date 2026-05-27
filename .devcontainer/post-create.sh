#!/usr/bin/env bash
set -euo pipefail

# Les volumes Docker mont s sur /home/vscode/.cache/* laissent le parent .cache en root.
# Resultat : pip ne peut plus ecrire son cache. On reaffecte tout a vscode.
sudo chown -R vscode:vscode /home/vscode/.cache

# L'image MS embarque un dépôt APT yarn dont la clé GPG est cassée — fait planter
# `apt-get update` (utilise par playwright install --with-deps). On le retire.
sudo rm -f /etc/apt/sources.list.d/yarn.list

echo "==> Creating .venv"
python -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip

# torch CPU-only : evite ~3 Go de libs CUDA inutiles (devcontainer sans GPU,
# CLIP ViT-B/32 tourne tres bien en CPU pour quelques centaines de photos).
echo "==> Installing torch (CPU-only)"
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

echo "==> Installing requirements.txt"
pip install -r requirements.txt

echo "==> Installing CLIP from GitHub"
pip install git+https://github.com/openai/CLIP.git

echo "==> Installing Playwright Chromium + system deps"
playwright install --with-deps chromium

echo "==> Creating runtime directories"
mkdir -p data/raw data/output logs

echo "==> Done. Recharge VS Code : l'interpreteur .venv sera detecte automatiquement."

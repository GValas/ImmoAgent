#!/usr/bin/env bash
set -euo pipefail

echo "==> Installing Python dependencies"
pip install --user --upgrade pip
pip install --user -r requirements.txt

echo "==> Installing CLIP from GitHub"
pip install --user git+https://github.com/openai/CLIP.git

echo "==> Installing Playwright system dependencies + Chromium"
# --with-deps installe les libs systeme (libnss3, libatk, etc.) requises par Chromium.
sudo $(which playwright) install-deps chromium || playwright install-deps chromium || true
playwright install chromium

echo "==> Creating runtime directories"
mkdir -p data/raw data/output logs

echo "==> Done. Lance le pipeline avec:  python orchestrator.py"

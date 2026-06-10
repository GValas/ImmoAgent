# ============================================================
# immo-agent — image de DEVCONTAINER (inspirée de MyMathsBook)
# Tout est installé à la construction de l'image, plutôt que via
# un post-create.sh : environnement reproductible et démarrage rapide.
#
# Image légère, 100 % CPU : le matching qualitatif est délégué à Ollama (conteneur
# dédié en prod, cf. docker-compose.yml) → plus de torch/sentence-transformers.
# La stack Python + Playwright est MUTUALISÉE avec le Dockerfile de prod via
# scripts/install-stack.sh (source unique de vérité).
# Spécifique au dev : user vscode + Claude Code CLI (pas de CMD : VS Code pilote).
# Pour tester le NLP en dev, pointer OLLAMA_HOST vers un Ollama joignable.
# ============================================================
FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

ARG USERNAME=vscode

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Europe/Paris

# L'image MS embarque un dépôt APT yarn à la clé GPG cassée → casse apt-get
# update (utilisé par `playwright install --with-deps`). On le retire.
RUN rm -f /etc/apt/sources.list.d/yarn.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# --- Stack Python + Playwright system-wide (script mutualisé avec la prod).
#     Pas de venv : le bind-mount du workspace masquerait un .venv construit ici.
#     cu126 (GPU) par défaut, comme la prod. Exécuté en root (playwright --with-deps). ---
COPY requirements.txt scripts/install-stack.sh /tmp/install/
RUN cd /tmp/install && bash install-stack.sh && rm -rf /tmp/install

# --- Outils par-utilisateur : Claude Code CLI (binaire natif, pas de Node) dans
#     ~/.local/bin de vscode, déjà mis dans le PATH via devcontainer.json. ---
USER ${USERNAME}
ENV HOME=/home/${USERNAME}
ENV PATH="/home/${USERNAME}/.local/bin:${PATH}"
RUN curl -fsSL https://claude.ai/install.sh | bash

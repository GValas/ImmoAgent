# ============================================================
# immo-agent — image de prod pour le scheduler (boucle continue)
# Stack Python partagée avec le devcontainer via scripts/install-stack.sh
# ============================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Europe/Paris

WORKDIR /app

# --- Dépendances système (curl/git utilitaires, tzdata pour l'heure FR) ---
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

# --- Stack Python + Playwright (couche cachée tant que requirements.txt et le
#     script ne changent pas). scripts/install-stack.sh est MUTUALISÉ avec le
#     devcontainer (.devcontainer/Dockerfile) → source unique de vérité.
#     Image légère, 100 % CPU : le matching qualitatif est délégué au conteneur
#     `ollama` (cf. docker-compose.yml), plus de torch ni de modèle embarqué. ---
COPY requirements.txt requirements.lock scripts/install-stack.sh ./
RUN bash install-stack.sh && rm -f install-stack.sh

# --- Code applicatif (en dernier : invalide le moins de couches au rebuild) ---
# CACHEBUST : run_prod.sh passe un timestamp → cette couche (et donc le code) est
# TOUJOURS reconstruite, alors que les couches lourdes ci-dessus (deps Python,
# chromium) restent en cache tant que requirements ne changent pas. Garantit du
# code à jour sans rebuild complet.
ARG CACHEBUST=0
COPY . .

# Répertoires runtime au cas où ils ne seraient pas montés
RUN mkdir -p data/raw data/output logs

# Le scheduler est une boucle infinie : c'est le PID 1 du conteneur.
# --once n'est PAS utilisé ici (réservé au debug à la main).
CMD ["python", "scheduler.py"]

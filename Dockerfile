# ============================================================
# immo-agent — image de prod pour le scheduler (boucle continue)
# Dérivée de .devcontainer/post-create.sh
# ============================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Europe/Paris

WORKDIR /app

# --- Dépendances système (git pour CLIP depuis GitHub, tzdata pour l'heure FR) ---
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

# --- Dépendances Python (couche cachée tant que requirements.txt ne change pas) ---
COPY requirements.txt .
# torch CUDA (cu124) EN PREMIER : les wheels embarquent le runtime CUDA (cudart,
# cuDNN, cuBLAS) — pas besoin d'image de base CUDA, mais l'hôte doit fournir le
# driver NVIDIA + nvidia-container-toolkit et le conteneur doit recevoir --gpus all.
# Pour repasser en CPU : remplacer cu124 par cpu.
RUN pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124 \
 && pip install -r requirements.txt \
 && pip install git+https://github.com/openai/CLIP.git

# --- Playwright Chromium + libs système (apt) ---
RUN playwright install --with-deps chromium

# --- Pré-cache du modèle CLIP ViT-B/32 (~340 Mo) pour un démarrage 100% offline ---
RUN python -c "import clip; clip.load('ViT-B/32', device='cpu')"

# --- Code applicatif (en dernier : invalide le moins de couches au rebuild) ---
COPY . .

# Répertoires runtime au cas où ils ne seraient pas montés
RUN mkdir -p data/raw data/output logs

# Le scheduler est une boucle infinie : c'est le PID 1 du conteneur.
# --once n'est PAS utilisé ici (réservé au debug à la main).
CMD ["python", "scheduler.py"]

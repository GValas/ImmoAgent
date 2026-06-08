#!/usr/bin/env bash
# SessionStart hook — annonce l'agent scraper-scout à l'ouverture d'une session
# Claude sur le projet immo-agent, sans interrompre le travail en cours.
# Émet un additionalContext (non bloquant) que le main agent affiche en 1 ligne.
#
# Référencé par .claude/settings.json (hooks.SessionStart).

set -euo pipefail

# Nombre de sources déjà référencées (contexte rapide, best-effort).
SRC="$(dirname "$0")/../config/sources.yaml"
count=0
if [ -f "$SRC" ]; then
  count="$(grep -c '^  - id:' "$SRC" 2>/dev/null || echo 0)"
fi

read -r -d '' MSG <<EOF || true
Agent disponible : **scraper-scout** — prospecte des sites immobiliers FR non
encore référencés (config/sources.yaml contient déjà ${count} sources) et génère
des scrapers prêts à intégrer au pipeline (modèle scrapers/le_tuc.py, test
standalone, 0 fuite dept). Au tout début de cette session UNIQUEMENT, propose à
l'utilisateur en UNE ligne de le lancer (délègue via l'agent scraper-scout). S'il
a déjà donné une autre tâche, n'insiste pas : enchaîne sur sa demande.
EOF

# additionalContext : injecté dans le contexte de démarrage, non bloquant.
printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":%s}}\n' \
  "$(printf '%s' "$MSG" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"

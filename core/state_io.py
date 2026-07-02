"""core/state_io.py — Lecture/écriture ATOMIQUE des fichiers d'état JSON.

Les fichiers d'état (biens_vus.json, scheduler_state.json, suivi_actif.json,
scraper_health.json) étaient écrits via un simple write_text : un kill au milieu
de l'écriture laissait un JSON tronqué qui, au redémarrage, faisait planter le
scheduler ou désactivait silencieusement la fonctionnalité. Ici :

  - atomic_write_json : écrit dans un .tmp puis rename (atomique sur POSIX) —
    le fichier cible est toujours soit l'ancienne version, soit la nouvelle ;
  - load_json : charge un JSON en retombant sur `default` si le fichier est
    absent OU corrompu (avec un warning, jamais d'exception).
"""
from __future__ import annotations

import json
from pathlib import Path


def load_json(path: Path, default):
    """Charge `path` en JSON ; retourne `default` si absent ou illisible."""
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[State] ⚠️  {path.name} illisible ({type(e).__name__}: {e}) — "
              f"valeur par défaut utilisée")
        return default


def atomic_write_json(path: Path, data, **dumps_kwargs) -> None:
    """Écrit `data` en JSON à `path` via un fichier temporaire + rename atomique."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, **dumps_kwargs), encoding="utf-8")
    tmp.replace(path)

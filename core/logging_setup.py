"""core/logging_setup.py — Horodatage centralisé des prints.

Le projet journalise via `print(f"[Worker] message")` (convention historique, pas
de logger configuré). L'horodatage automatique des lignes `[…]` était dupliqué
verbatim dans orchestrator.py et scheduler.py (avec un monkeypatch de
`builtins.print`). On centralise ici cette unique fonction ; les deux points
d'entrée l'appellent au lieu de redéfinir le patch.
"""
from __future__ import annotations

import builtins as _builtins
from datetime import datetime

_installed = False


def enable_timestamped_prints() -> None:
    """Préfixe d'un HH:MM:SS toute ligne imprimée commençant par '[' (les logs
    `[Worker] …`). Idempotent : un second appel ne ré-empile pas le patch."""
    global _installed
    if _installed:
        return
    orig_print = _builtins.print

    def _ts_print(*args, **kwargs):
        if args and isinstance(args[0], str) and args[0].startswith("["):
            ts = datetime.now().strftime("%H:%M:%S")
            orig_print(f"{ts} {args[0]}", *args[1:], **kwargs)
        else:
            orig_print(*args, **kwargs)

    _builtins.print = _ts_print
    _installed = True

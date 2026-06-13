"""core/dedup.py — Clé de déduplication unique.

Auparavant réimplémentée trois fois (models.Bien.hash_dedup, hunter.deduplicate
inline, scheduler.bien_hash) avec un risque de divergence silencieuse. Désormais
une seule définition, réutilisée partout.
"""
from __future__ import annotations

import hashlib


def dedup_key(prix, surface, ville) -> str:
    """Chaîne de déduplication brute « prix-surface-ville » (ville normalisée).

    `ville` peut être None (scrapers sans ville exposée) → `or ''` couvre clé
    absente ET valeur None."""
    ville_norm = str(ville or "").lower().strip()
    return f"{prix}-{surface}-{ville_norm}"


def dedup_hash(bien: dict) -> str:
    """Hash md5 de la clé de déduplication d'un bien (prix + surface + ville)."""
    key = dedup_key(bien.get("prix"), bien.get("surface"), bien.get("ville"))
    return hashlib.md5(key.encode()).hexdigest()

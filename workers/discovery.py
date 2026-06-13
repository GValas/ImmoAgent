"""
workers/discovery.py — Worker 1 : Discovery
Charge les sources depuis sources.yaml.

Sans appel API — la gestion des sources se fait de deux façons :
1. Éditer sources.yaml manuellement
2. Demander à Claude Code : "ajoute la source X dans sources.yaml"

Claude Code peut enrichir sources.yaml en autonomie lors des sessions
de maintenance (nouvelles sources, désactivation de sites cassés, etc.)
"""
from pathlib import Path

from config_loader import load_criteria, load_sources

SOURCES_PATH = Path(__file__).parent.parent / "config" / "sources.yaml"


async def run(criteres=None) -> list[dict]:
    """
    Retourne les sources actives depuis sources.yaml.
    Filtre par couverture départementale si renseignée.
    """
    if criteres is None:
        criteres = load_criteria()

    sources = load_sources()

    # Filtre optionnel : garder les sources qui couvrent au moins un département cible
    # (si la source n'a pas de champ 'couverture', elle est considérée nationale)
    filtered = []
    for s in sources:
        couverture = s.get("couverture", [])
        if not couverture:
            filtered.append(s)  # source nationale, toujours incluse
        elif any(dep in couverture for dep in criteres.departements):
            filtered.append(s)

    print(f"[Discovery] {len(filtered)} source(s) active(s) chargée(s) depuis sources.yaml")
    for s in filtered:
        print(f"  → {s['nom']} ({s['methode']}) — priorité {s.get('priorite', '?')}")

    if not filtered:
        print("[Discovery] Aucune source active.")
        print("[Discovery] Édite config/sources.yaml ou demande à Claude Code d'en ajouter.")

    return filtered


def add_source(source: dict):
    """
    Ajoute une source dans sources.yaml.
    Appelé par Claude Code lors des sessions de maintenance.
    """
    import yaml
    data = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8")) if SOURCES_PATH.exists() else {"sources": []}
    existing_ids = {s["id"] for s in data.get("sources", [])}
    if source["id"] in existing_ids:
        print(f"[Discovery] Source '{source['id']}' déjà présente — skip")
        return
    data.setdefault("sources", []).append(source)
    SOURCES_PATH.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False))
    print(f"[Discovery] Source '{source['nom']}' ajoutée dans sources.yaml")


if __name__ == "__main__":
    import asyncio
    asyncio.run(run())

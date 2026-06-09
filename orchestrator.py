"""
orchestrator.py — Chef d'orchestre
Séquence les 4 workers et gère le pipeline complet.

Usage :
  python orchestrator.py                   # pipeline complet
  python orchestrator.py --skip-discovery  # réutilise sources.yaml existant
  python orchestrator.py --skip-build      # scrapers déjà générés
  python orchestrator.py --only-analyse    # réanalyse le dernier fichier raw
"""
import asyncio
import argparse
import json
import builtins as _builtins
from datetime import datetime
from pathlib import Path

from config_loader import load_criteria, load_sources
from workers import discovery, builder, hunter, analyst

# ── Timestamps automatiques sur tous les prints [Worker] ──────────────────
_orig_print = _builtins.print


def _ts_print(*args, **kwargs):
    if args and isinstance(args[0], str) and args[0].startswith("["):
        ts = datetime.now().strftime("%H:%M:%S")
        _orig_print(f"{ts} {args[0]}", *args[1:], **kwargs)
    else:
        _orig_print(*args, **kwargs)


_builtins.print = _ts_print
# ─────────────────────────────────────────────────────────────────────────


async def run_pipeline(
    skip_discovery: bool = False,
    skip_build: bool = False,
    only_analyse: bool = False,
):
    start = datetime.now()
    print("=" * 60)
    print("  IMMO-AGENT — Pipeline de recherche immobilière")
    print(f"  Démarrage : {start.strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    # ── Chargement des critères ──
    criteres = load_criteria()
    print(f"\n📋 Critères chargés :")
    print(f"   Départements : {', '.join(criteres.departements)}")
    print(f"   Types        : {', '.join(criteres.types_bien)}")
    print(f"   Budget max   : {criteres.prix_max:,} €")
    print(f"   Surface      : {criteres.surface_min}–{criteres.surface_max} m²\n")

    if only_analyse:
        raw_files = sorted(Path("data/raw").glob("biens_raw_*.json"))
        if not raw_files:
            print("❌ Aucun fichier raw trouvé.")
            return
        biens_bruts = json.loads(raw_files[-1].read_text(encoding="utf-8"))
        print(f"⏭  Re-analyse de {raw_files[-1].name} ({len(biens_bruts)} biens)")
        output = await analyst.run(biens_bruts, criteres)
        _print_done(start, output)
        return

    # ── Worker 1 : Discovery ──
    if skip_discovery:
        print("⏭  Discovery skippé — chargement depuis sources.yaml")
        sources = load_sources()
    else:
        print("🔍 [1/4] Worker Discovery — identification des sources...")
        sources = await discovery.run(criteres)
        if not sources:
            print("❌ Aucune source trouvée. Arrêt.")
            return

    # ── Worker 2 : Builder ──
    if skip_build:
        print("⏭  Builder skippé — scrapers existants utilisés")
    else:
        print(f"\n🔧 [2/4] Worker Builder — génération de {len(sources)} scrapers...")
        await builder.run(sources, criteres)

    # ── Worker 3 : Hunter ──
    print(f"\n🏹 [3/4] Worker Hunter — lancement des recherches...")
    biens_bruts = await hunter.run(sources, criteres)

    if not biens_bruts:
        print("❌ Aucun bien récupéré. Vérifie les scrapers.")
        return

    # ── Worker 4 : Analyst ──
    print(f"\n📊 [4/4] Worker Analyst — enrichissement et agrégation...")
    output = await analyst.run(biens_bruts, criteres)

    _print_done(start, output)


def _print_done(start: datetime, output: Path):
    elapsed = (datetime.now() - start).seconds
    print("\n" + "=" * 60)
    print(f"  ✅ Pipeline terminé en {elapsed}s")
    print(f"  📁 Résultats : {output}")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Immo-Agent orchestrator")
    parser.add_argument("--skip-discovery", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--only-analyse", action="store_true",
                        help="Ré-enrichit le dernier raw sans re-scraper ni re-filtrer")
    args = parser.parse_args()

    asyncio.run(run_pipeline(
        skip_discovery=args.skip_discovery,
        skip_build=args.skip_build,
        only_analyse=args.only_analyse,
    ))

"""
orchestrator.py — Chef d'orchestre
Séquence les 4 workers et gère le pipeline complet.

Usage :
  python orchestrator.py                   # pipeline complet
  python orchestrator.py --skip-discovery  # réutilise sources.yaml existant
  python orchestrator.py --skip-build      # scrapers déjà générés
  python orchestrator.py --only-analyse    # re-filtre (a posteriori) + ré-analyse le dernier raw
"""
import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from config_loader import load_criteria, load_sources
from core.filters import apply_posterior_filters
from core.logging_setup import enable_timestamped_prints
from workers import analyst, builder, discovery, hunter

# Horodatage automatique des logs `[Worker] …` (centralisé dans core.logging_setup).
enable_timestamped_prints()


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
    print("\n📋 Critères chargés :")
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

        # Ré-applique le filtrage A POSTERIORI (sur données déjà scrapées+enrichies)
        # avec les critères COURANTS : structurel (prix/surface/pièces/DPE), terrain
        # depuis le texte, mots-clés (obligatoires/interdits) et photos_min — séquence
        # unique partagée (core.filters). Répercute un changement de criteria.md sur
        # suivi_actif SANS re-scraper le web.
        before = len(biens_bruts)
        # dept_guard=True : même garde-fou hors-zone que refilter_suivi (un raw
        # historique peut contenir des fuites de scrapers depuis corrigés).
        biens_bruts = apply_posterior_filters(biens_bruts, criteres, dept_guard=True)
        print(f"⏪ Re-filtrage a posteriori : {len(biens_bruts)}/{before} biens conservés")

        if not biens_bruts:
            print("❌ Plus aucun bien après re-filtrage.")
            return
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
    print("\n🏹 [3/4] Worker Hunter — lancement des recherches...")
    biens_bruts = await hunter.run(sources, criteres)

    if not biens_bruts:
        print("❌ Aucun bien récupéré. Vérifie les scrapers.")
        return

    # ── Worker 4 : Analyst ──
    print("\n📊 [4/4] Worker Analyst — enrichissement et agrégation...")
    output = await analyst.run(biens_bruts, criteres)

    _print_done(start, output)


def _print_done(start: datetime, output: Path):
    elapsed = int((datetime.now() - start).total_seconds())
    print("\n" + "=" * 60)
    print(f"  ✅ Pipeline terminé en {elapsed}s")
    print(f"  📁 Résultats : {output}")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Immo-Agent orchestrator")
    parser.add_argument("--skip-discovery", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--only-analyse", action="store_true",
                        help="Ré-applique le filtrage a posteriori (structurel + mots-clés "
                             "+ photos_min) au dernier raw et régénère suivi_actif, sans re-scraper")
    args = parser.parse_args()

    asyncio.run(run_pipeline(
        skip_discovery=args.skip_discovery,
        skip_build=args.skip_build,
        only_analyse=args.only_analyse,
    ))

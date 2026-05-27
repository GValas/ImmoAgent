"""
scheduler.py — Pipeline continu immo-agent
Tourne indéfiniment, déclenche chaque agent selon sa fréquence propre.

Fréquences (configurables dans criteria.md) :
  Hunter + Analyst   → toutes les N heures  (nouvelles annonces)
  Discovery          → tous les N jours     (re-qualifier les sources)
  Builder            → tous les N jours     (scrapers à jour si sites changent)

Diff :
  Seules les nouvelles annonces (jamais vues) sont scorées et ajoutées
  au fichier de suivi actif data/output/suivi_actif.xlsx.

Usage :
  python scheduler.py            # démarre le scheduler
  python scheduler.py --once     # un seul cycle complet puis stop
"""

import asyncio
import argparse
import json
import re
import hashlib
import builtins as _builtins
import yaml
from datetime import datetime, timedelta
from pathlib import Path

# ── Timestamps automatiques sur tous les prints [Agent] ──────────────────
_orig_print = _builtins.print


def _ts_print(*args, **kwargs):
    if args and isinstance(args[0], str) and args[0].startswith("["):
        ts = datetime.now().strftime("%H:%M:%S")
        _orig_print(f"{ts} {args[0]}", *args[1:], **kwargs)
    else:
        _orig_print(*args, **kwargs)


_builtins.print = _ts_print
# ─────────────────────────────────────────────────────────────────────────

from config_loader import load_criteria, load_sources
from agents import discovery, builder, hunter, analyst

DATA_DIR   = Path("data")
RAW_DIR    = DATA_DIR / "raw"
OUTPUT_DIR = DATA_DIR / "output"
STATE_FILE = DATA_DIR / "scheduler_state.json"
SEEN_FILE  = DATA_DIR / "biens_vus.json"
SUIVI_FILE = OUTPUT_DIR / "suivi_actif.xlsx"

CRITERIA_MD = Path("config/criteria.md")


# ──────────────────────────────────────────────
# CONFIG SCHEDULER DEPUIS criteria.md
# ──────────────────────────────────────────────

def load_scheduler_config() -> dict:
    """Lit les paramètres scheduler depuis criteria.md."""
    defaults = {
        "hunter_interval_hours":   4,
        "discovery_interval_days": 7,
        "builder_interval_days":   30,
        "score_seuil_interet":     65,
        "max_biens_suivi":         50,
    }
    try:
        content = CRITERIA_MD.read_text(encoding="utf-8")
        blocks = re.findall(r"```\s*([\s\S]*?)```", content)
        for block in blocks:
            for line in block.strip().splitlines():
                line = line.split("#")[0].strip()
                if ":" in line:
                    key, val = line.split(":", 1)
                    key = key.strip()
                    if key in defaults:
                        try:
                            defaults[key] = yaml.safe_load(val.strip())
                        except Exception:
                            pass
    except Exception:
        pass
    return defaults


# ──────────────────────────────────────────────
# STATE — dernière exécution de chaque agent
# ──────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"last_hunter": None, "last_discovery": None, "last_builder": None}


def save_state(state: dict):
    DATA_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def should_run(last_run_str: str | None, interval_hours: float) -> bool:
    if last_run_str is None:
        return True
    last = datetime.fromisoformat(last_run_str)
    return datetime.now() >= last + timedelta(hours=interval_hours)


# ──────────────────────────────────────────────
# DÉDUPLICATION INTER-RUNS (biens déjà vus)
# ──────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen(seen: set):
    DATA_DIR.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps(list(seen)), encoding="utf-8")


def bien_hash(b: dict) -> str:
    key = f"{b.get('prix')}-{b.get('surface')}-{b.get('ville','').lower().strip()}"
    return hashlib.md5(key.encode()).hexdigest()


def filter_new(biens: list[dict], seen: set) -> tuple[list[dict], set]:
    """Retourne uniquement les biens jamais vus, et le seen mis à jour."""
    new, updated_seen = [], set(seen)
    for b in biens:
        h = bien_hash(b)
        if h not in updated_seen:
            new.append(b)
            updated_seen.add(h)
    return new, updated_seen


# ──────────────────────────────────────────────
# MISE À JOUR DU FICHIER DE SUIVI ACTIF
# ──────────────────────────────────────────────

def update_suivi(new_biens_scored: list[dict], cfg: dict):
    """
    Fusionne les nouveaux biens scorés dans suivi_actif.xlsx.
    Garde uniquement les max_biens_suivi meilleurs au-dessus du seuil.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    seuil      = cfg["score_seuil_interet"]
    max_biens  = cfg["max_biens_suivi"]

    # Charger le suivi existant
    existing = []
    suivi_json = DATA_DIR / "suivi_actif.json"
    if suivi_json.exists():
        existing = json.loads(suivi_json.read_text(encoding="utf-8"))

    # Ajouter les nouveaux au-dessus du seuil
    qualifying = [b for b in new_biens_scored if (b.get("score_total") or 0) >= seuil]
    merged = existing + qualifying

    # Tri par score décroissant, déduplique, tronque
    seen_h = set()
    deduped = []
    for b in sorted(merged, key=lambda x: x.get("score_total") or 0, reverse=True):
        h = bien_hash(b)
        if h not in seen_h:
            deduped.append(b)
            seen_h.add(h)

    deduped = deduped[:max_biens]

    # Sauvegarder JSON intermédiaire
    suivi_json.write_text(json.dumps(deduped, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # Regénérer l'Excel
    _write_suivi_excel(deduped, seuil)
    print(f"[Scheduler] Suivi actif : {len(deduped)} biens (seuil {seuil}+)")


def _write_suivi_excel(biens: list[dict], seuil: int):
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("[Scheduler] openpyxl manquant — Excel non généré")
        return

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Suivi actif"

    headers = [
        "Score", "Score visuel", "Ajouté le", "Source", "Titre",
        "Ville", "Dép", "Surface", "Terrain", "Pièces", "DPE",
        "Prix (€)", "Prix/m²", "Prix/m² marché", "Résumé style", "Alertes", "URL"
    ]
    hfill = PatternFill("solid", fgColor="2C3E50")
    hfont = Font(color="FFFFFF", bold=True)
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=col, value=h)
        c.fill = hfill
        c.font = hfont
        c.alignment = Alignment(horizontal="center")

    fills = {
        "high":   PatternFill("solid", fgColor="D5F5E3"),
        "medium": PatternFill("solid", fgColor="FEF9E7"),
    }

    for row, b in enumerate(biens, 2):
        score = b.get("score_total", 0)
        fill  = fills["high"] if score >= 75 else fills["medium"]
        vals  = [
            score,
            b.get("score_visuel"),
            b.get("date_scraped", "")[:10],
            b.get("source", ""),
            b.get("titre", ""),
            b.get("ville", ""),
            b.get("departement", ""),
            b.get("surface"),
            b.get("surface_terrain"),
            b.get("pieces"),
            b.get("dpe", ""),
            b.get("prix"),
            b.get("prix_m2_calcule"),
            b.get("prix_m2_marche_dep"),
            b.get("resume_visuel", ""),
            " | ".join(b.get("alerte", [])),
            b.get("url", ""),
        ]
        url_col = len(headers)  # dernière colonne = URL
        for col, v in enumerate(vals, 1):
            c = ws.cell(row=row, column=col, value=v)
            c.fill = fill
            if col == url_col and v:
                c.hyperlink = str(v)
                c.value = "Voir l'annonce"
                c.style = "Hyperlink"

    widths = [8, 12, 12, 12, 40, 18, 6, 9, 9, 8, 6, 12, 10, 14, 40, 35, 50]
    for col, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(col)].width = w

    ws.auto_filter.ref = ws.dimensions
    ws.freeze_panes = "A2"

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    ws2 = wb.create_sheet("Infos")
    ws2["A1"] = f"Dernière mise à jour : {ts}"
    ws2["A2"] = f"Seuil de score : {seuil}+"
    ws2["A3"] = f"Nombre de biens suivis : {len(biens)}"

    wb.save(SUIVI_FILE)
    print(f"[Scheduler] Excel mis à jour → {SUIVI_FILE}")


# ──────────────────────────────────────────────
# CYCLE PRINCIPAL
# ──────────────────────────────────────────────

async def run_cycle(state: dict, cfg: dict, sources: list[dict]) -> dict:
    """Exécute un cycle du scheduler selon l'état et les fréquences."""
    criteres = load_criteria()
    now      = datetime.now().isoformat()
    ran_something = False

    # ── Discovery ──
    if should_run(state["last_discovery"], cfg["discovery_interval_days"] * 24):
        print(f"\n[Scheduler] {_ts()} Discovery...")
        sources = await discovery.run(criteres)
        state["last_discovery"] = now
        ran_something = True

    # ── Builder ──
    if should_run(state["last_builder"], cfg["builder_interval_days"] * 24):
        print(f"[Scheduler] {_ts()} Builder...")
        await builder.run(sources, criteres)
        state["last_builder"] = now
        ran_something = True

    # ── Hunter + Vision + Analyst ──
    if should_run(state["last_hunter"], cfg["hunter_interval_hours"]):
        print(f"[Scheduler] {_ts()} Hunter...")
        biens_bruts = await hunter.run(sources, criteres)

        if biens_bruts:
            # Filtrer les biens déjà vus
            seen = load_seen()
            new_biens, seen = filter_new(biens_bruts, seen)
            save_seen(seen)

            print(f"[Scheduler] {len(new_biens)} nouveau(x) bien(s) sur {len(biens_bruts)}")

            if new_biens:
                print(f"[Scheduler] {_ts()} Analyst sur {len(new_biens)} nouveaux biens...")
                scored = []
                from agents.analyst import score_bien, fetch_prix_marche_dvf
                prix_marche = await fetch_prix_marche_dvf(criteres.departements)
                criteres_dict = {
                    "surface_min":   criteres.surface_min,
                    "surface_max":   criteres.surface_max,
                    "terrain_min":   criteres.terrain_min,
                    "poids_scoring": criteres.poids_scoring,
                }
                for b in new_biens:
                    scored.append(score_bien(b, criteres_dict, prix_marche))

                update_suivi(scored, cfg)
            else:
                print("[Scheduler] Aucun nouveau bien — suivi inchangé")
        else:
            print("[Scheduler] Aucun bien récupéré ce cycle")

        state["last_hunter"] = now
        ran_something = True

    if not ran_something:
        print(f"[Scheduler] {_ts()} Rien à faire ce tick")

    return state


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _next_run(state: dict, cfg: dict) -> datetime:
    """Calcule le prochain moment où quelque chose doit tourner."""
    candidates = []
    if state["last_hunter"]:
        candidates.append(
            datetime.fromisoformat(state["last_hunter"])
            + timedelta(hours=cfg["hunter_interval_hours"])
        )
    else:
        candidates.append(datetime.now())
    return min(candidates) if candidates else datetime.now()


# ──────────────────────────────────────────────
# BOUCLE INFINIE
# ──────────────────────────────────────────────

async def run_forever():
    print("=" * 55)
    print("  IMMO-AGENT Scheduler — démarrage")
    print("=" * 55)

    DATA_DIR.mkdir(exist_ok=True)
    state   = load_state()
    cfg     = load_scheduler_config()
    sources = load_sources()

    print(f"  Hunter    : toutes les {cfg['hunter_interval_hours']}h")
    print(f"  Discovery : tous les {cfg['discovery_interval_days']}j")
    print(f"  Builder   : tous les {cfg['builder_interval_days']}j")
    print(f"  Seuil     : score >= {cfg['score_seuil_interet']}")
    print("=" * 55)

    while True:
        cfg   = load_scheduler_config()   # relit criteria.md à chaque cycle
        state = await run_cycle(state, cfg, sources)
        save_state(state)

        # Calcul du prochain tick
        next_run = _next_run(state, cfg)
        wait_sec = max(0, (next_run - datetime.now()).total_seconds())
        print(f"[Scheduler] Prochain cycle dans {int(wait_sec // 3600)}h{int((wait_sec % 3600) // 60)}m")
        await asyncio.sleep(wait_sec)


async def run_once():
    """Un seul cycle complet — utile pour tester."""
    DATA_DIR.mkdir(exist_ok=True)
    state   = {"last_hunter": None, "last_discovery": None, "last_builder": None}
    cfg     = load_scheduler_config()
    sources = load_sources()
    state   = await run_cycle(state, cfg, sources)
    save_state(state)
    print("\n[Scheduler] Cycle unique terminé.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Un seul cycle puis stop")
    args = parser.parse_args()

    if args.once:
        asyncio.run(run_once())
    else:
        asyncio.run(run_forever())

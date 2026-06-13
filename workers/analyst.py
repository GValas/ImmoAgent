"""
workers/analyst.py — Worker 4 : Analyst
Enrichit chaque bien (DVF, match qualitatif NLP, alertes), agrège dans un Excel final.
(Le scoring pondéré a été retiré — à revoir plus tard.)
"""
import asyncio
import json

# Import DVF scraper pour les prix de référence réels (CSV data.gouv.fr)
import sys as _sys
from datetime import datetime
from pathlib import Path

_sys.path.insert(0, str(Path(__file__).parent.parent))
from core.dept_data import DEPT_NOMS, dept_nom  # noqa: F401 (réexport pour scheduler)
from core.excel_export import RESULTATS_COLUMNS, write_listings_xlsx
from scrapers.dvf import get_prix_m2_reference as _dvf_get_prix_m2

OUTPUT_DIR = Path(__file__).parent.parent / "data" / "output"

# ──────────────────────────────────────────────
# ENRICHISSEMENT FACTUEL
# (le scoring pondéré a été retiré — à revoir plus tard)
# ──────────────────────────────────────────────

def enrich_bien(bien: dict, prix_m2_marche: dict) -> dict:
    """
    Enrichit un bien avec des données factuelles d'affichage : prix/m², prix/m²
    marché (DVF) et alertes (bonne affaire, DPE passoire, travaux probables,
    correspondance qualitative). Pas de score pondéré.
    """
    alertes = []

    # --- Prix vs marché (DVF) ---
    dep = bien.get("departement", "")
    prix_m2_ref = prix_m2_marche.get(dep, 0)
    prix_m2_bien = bien.get("prix_m2") or (
        bien["prix"] / bien["surface"] if bien.get("prix") and bien.get("surface") else None
    )
    if prix_m2_bien and prix_m2_ref:
        ratio = prix_m2_ref / prix_m2_bien  # >1 = bonne affaire
        if ratio > 1.3:
            alertes.append("🟢 Prix très inférieur au marché")
        elif ratio < 0.8:
            alertes.append("🔴 Prix supérieur au marché")

    # --- DPE ---
    dpe = (bien.get("dpe") or "").upper()
    if dpe in ["F", "G"]:
        alertes.append(f"⚠️ DPE {dpe} — passoire thermique")

    # --- État (proxy : mots-clés dans description) ---
    desc = ((bien.get("description") or "") + " " + (bien.get("titre") or "")).lower()
    if any(w in desc for w in ["travaux", "rénover", "à rafraîchir"]):
        alertes.append("🔧 Travaux probables")

    # --- Match qualitatif (NLP) — annoté en amont par workers.qualitative ---
    mq = bien.get("match_qualitatif")
    if mq is not None and mq >= 60:
        alertes.append("✨ Colle à la description recherchée")

    bien["alerte"] = alertes
    bien["prix_m2_calcule"] = round(prix_m2_bien, 0) if prix_m2_bien else None
    bien["prix_m2_marche_dep"] = prix_m2_ref or None
    return bien


# ──────────────────────────────────────────────
# DVF — prix marché par département
# ──────────────────────────────────────────────

async def fetch_prix_marche_dvf(departements: list[str]) -> dict:
    """
    Récupère le prix médian €/m² réel par département via les fichiers CSV DVF (data.gouv.fr).
    Télécharge les données de transactions passées (maisons 2024) et calcule la médiane.
    Retourne {dep_code: prix_m2_median}.
    """
    try:
        result = await _dvf_get_prix_m2(departements)
        print(f"[Analyst] Prix DVF réels chargés pour {len(result)} depts")
        return result
    except Exception as e:
        print(f"[Analyst] DVF indisponible ({e}) — fallback valeurs de référence")
        return {dep: _prix_m2_reference(dep) for dep in departements}


def _prix_m2_reference(dep: str) -> int:
    """
    Prix de référence €/m² maison par département — fallback si DVF indisponible.
    Valeurs estimées 2024 pour les départements cibles (Loire, Centre, Sarthe).
    """
    refs = {
        # Départements cibles — Loire / Centre-Val-de-Loire / Pays-de-la-Loire
        "72": 1650,  # Sarthe
        "28": 1950,  # Eure-et-Loir
        "45": 1900,  # Loiret
        "89": 1400,  # Yonne
        "49": 2100,  # Maine-et-Loire
        "37": 2200,  # Indre-et-Loire
        "36": 1200,  # Indre
        "18": 1300,  # Cher
        "58": 1150,  # Nièvre
        "41": 1800,  # Loir-et-Cher
        "53": 1600,  # Mayenne
        # Autres grandes zones
        "06": 5200, "83": 4100, "84": 2800, "13": 3500,
        "69": 4800, "75": 10500, "92": 7200, "94": 5100,
        "33": 3800, "34": 3600, "44": 3700, "31": 3400,
        "67": 3200, "76": 2600, "59": 2400, "38": 3000,
    }
    return refs.get(dep, 2000)  # défaut national réaliste (non plus 3000)


# ──────────────────────────────────────────────
# RÉSUMÉ LLM
# ──────────────────────────────────────────────

def llm_summary(top_biens: list[dict]) -> str:
    """
    Génère un résumé texte local des meilleures opportunités.
    Sans appel API — pour un résumé enrichi, demande à Claude Code :
    "Analyse data/output/resultats_xxx.xlsx et résume les meilleures opportunités"
    """
    if not top_biens:
        return "Aucun bien scoré disponible."

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lignes = [f"Résumé généré le {ts} — {len(top_biens)} bien(s) analysé(s)\n"]

    for i, b in enumerate(top_biens[:5], 1):
        mq     = b.get("match_qualitatif")
        mq_str = f"[match {mq:.0f}] " if mq is not None else ""
        prix   = f"{b.get('prix', '?'):,} €" if b.get("prix") else "prix ?"
        surf   = f"{b.get('surface', '?')} m²"
        ville  = b.get("ville", "?")
        dep    = b.get("departement", "")
        dpe    = b.get("dpe", "?")
        alertes = b.get("alerte", [])
        alert_str = f" ⚠ {alertes[0]}" if alertes else ""
        lignes.append(
            f"#{i} {mq_str}{b.get('titre','')[:50]}\n"
            f"   {ville} ({dep}) — {surf} — {prix} — DPE {dpe}{alert_str}"
        )

    return "\n".join(lignes)


# ──────────────────────────────────────────────
# EXPORT EXCEL
# ──────────────────────────────────────────────


def export_excel(biens: list[dict], resume: str) -> Path:
    """Exporte les résultats dans data/output/resultats_<ts>.xlsx (writer partagé)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    path = OUTPUT_DIR / f"resultats_{ts}.xlsx"

    def _resume_sheet(wb):
        from openpyxl.styles import Alignment, Font
        ws2 = wb.create_sheet("Résumé")
        ws2["A1"] = "Résumé exécutif"
        ws2["A1"].font = Font(bold=True, size=14)
        ws2["A3"] = resume
        ws2["A3"].alignment = Alignment(wrap_text=True)
        ws2.column_dimensions["A"].width = 100

    try:
        write_listings_xlsx(
            biens, path,
            columns=RESULTATS_COLUMNS,
            sheet_title="Résultats",
            build_extra_sheet=_resume_sheet,
        )
    except ImportError as e:
        raise ImportError("pip install openpyxl") from e
    print(f"[Analyst] Excel exporté → {path}")
    return path


# ──────────────────────────────────────────────
# POINT D'ENTRÉE
# ──────────────────────────────────────────────

async def run(biens_bruts: list[dict], criteres) -> Path:
    """Pipeline : enrichissement DVF + match qualitatif NLP → export Excel.
    (Le scoring pondéré a été retiré — à revoir plus tard.)"""
    print(f"[Analyst] Enrichissement de {len(biens_bruts)} biens...")

    # Données marché DVF
    prix_marche = await fetch_prix_marche_dvf(criteres.departements)

    # Match qualitatif NLP (annote match_qualitatif/match_extrait) — si une
    # description qualitative est définie (sinon no-op).
    desc_qual = getattr(criteres, "description_qualitative", "") or ""
    if desc_qual.strip():
        from workers.qualitative import annotate_biens as qual_annotate
        await qual_annotate(biens_bruts, desc_qual)

    # Enrichissement factuel (prix/m², DVF, alertes)
    enriched = [enrich_bien(b, prix_marche) for b in biens_bruts]

    # Tri par match qualitatif décroissant (seul signal de pertinence restant) ;
    # ordre d'insertion si pas de description qualitative.
    if desc_qual.strip():
        enriched.sort(key=lambda b: b.get("match_qualitatif") or 0, reverse=True)
        print(f"[Analyst] Top 5 match qualitatif : "
              f"{[b.get('match_qualitatif') for b in enriched[:5]]}")

    # Résumé local
    resume = llm_summary(enriched[:10])
    print(f"\n--- RÉSUMÉ EXÉCUTIF ---\n{resume}\n")

    # Export
    path = export_excel(enriched, resume)
    return path


if __name__ == "__main__":
    import sys
    from pathlib import Path

    from config_loader import load_criteria

    raw_files = sorted(Path("data/raw").glob("*.json"))
    if not raw_files:
        print("Aucun fichier raw trouvé. Lance d'abord hunter.py")
        sys.exit(1)

    latest = raw_files[-1]
    print(f"Analyse de {latest}")
    biens = json.loads(latest.read_text(encoding="utf-8"))
    criteres = load_criteria()
    asyncio.run(run(biens, criteres))

"""
agents/analyst.py — Agent 4 : Analyst
Score chaque bien, enrichit avec données DVF réelles, agrège dans un Excel final.
"""
import json
import asyncio
from datetime import datetime
from pathlib import Path

# Import DVF scraper pour les prix de référence réels (CSV data.gouv.fr)
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.dvf import get_prix_m2_reference as _dvf_get_prix_m2

OUTPUT_DIR = Path(__file__).parent.parent / "data" / "output"

# ──────────────────────────────────────────────
# SCORING
# ──────────────────────────────────────────────

DPE_SCORE = {"A": 100, "B": 85, "C": 70, "D": 55, "E": 30, "F": 10, "G": 0}

def score_bien(bien: dict, criteres_dict: dict, prix_m2_marche: dict) -> dict:
    """
    Calcule un score pondéré sur 100 pour un bien.
    Retourne le bien enrichi avec score_total et score_detail.
    """
    poids = criteres_dict["poids_scoring"]
    scores = {}
    alertes = []

    # --- Prix vs marché ---
    dep = bien.get("departement", "")
    prix_m2_ref = prix_m2_marche.get(dep, 0)
    prix_m2_bien = bien.get("prix_m2") or (
        bien["prix"] / bien["surface"] if bien.get("prix") and bien.get("surface") else None
    )
    if prix_m2_bien and prix_m2_ref:
        ratio = prix_m2_ref / prix_m2_bien  # >1 = bonne affaire
        scores["prix"] = min(100, max(0, int(ratio * 50 + 50)))
        if ratio > 1.3:
            alertes.append("🟢 Prix très inférieur au marché")
        elif ratio < 0.8:
            alertes.append("🔴 Prix supérieur au marché")
    else:
        scores["prix"] = 50  # neutre si pas de données

    # --- Surface ---
    surface = bien.get("surface", 0) or 0
    s_min = criteres_dict.get("surface_min", 80)
    s_max = criteres_dict.get("surface_max", 300)
    if surface >= s_max:
        scores["surface"] = 100
    elif surface >= s_min:
        scores["surface"] = int((surface - s_min) / (s_max - s_min) * 100)
    else:
        scores["surface"] = 0

    # --- Terrain ---
    terrain = bien.get("surface_terrain", 0) or 0
    t_min = criteres_dict.get("terrain_min", 200)
    scores["terrain"] = min(100, int(terrain / max(t_min, 1) * 60)) if terrain else 20

    # --- DPE ---
    dpe = (bien.get("dpe") or "").upper()
    scores["dpe"] = DPE_SCORE.get(dpe, 50)
    if dpe in ["F", "G"]:
        alertes.append(f"⚠️ DPE {dpe} — passoire thermique")

    # --- Localisation (proxy : photos disponibles + adresse) ---
    has_photos = len(bien.get("photos", [])) > 0
    has_address = bool(bien.get("adresse") or bien.get("ville"))
    scores["localisation"] = (50 if has_address else 20) + (30 if has_photos else 0)

    # --- État (proxy : mots-clés dans description) ---
    desc = (bien.get("description", "") + bien.get("titre", "")).lower()
    if any(w in desc for w in ["neuf", "rénov", "refait", "récent"]):
        scores["etat"] = 90
    elif any(w in desc for w in ["travaux", "rénover", "à rafraîchir"]):
        scores["etat"] = 30
        alertes.append("🔧 Travaux probables")
    else:
        scores["etat"] = 60

    # --- Score total pondéré ---
    # Les clés poids_scoring ont le préfixe "poids_"
    total = sum(
        scores.get(k, 50) * poids.get(f"poids_{k}", poids.get(k, 0)) / 100
        for k in ["prix", "surface", "terrain", "localisation", "etat", "dpe"]
    )

    bien["score_total"] = round(total, 1)
    bien["score_detail"] = scores
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
        score  = b.get("score_total", 0)
        prix   = f"{b.get('prix', '?'):,} €" if b.get("prix") else "prix ?"
        surf   = f"{b.get('surface', '?')} m²"
        ville  = b.get("ville", "?")
        dep    = b.get("departement", "")
        dpe    = b.get("dpe", "?")
        sv     = b.get("score_visuel")
        sv_str = f" | style {sv:.0f}/100" if sv is not None else ""
        alertes = b.get("alerte", [])
        alert_str = f" ⚠ {alertes[0]}" if alertes else ""
        lignes.append(
            f"#{i} [{score:.0f}/100{sv_str}] {b.get('titre','')[:50]}\n"
            f"   {ville} ({dep}) — {surf} — {prix} — DPE {dpe}{alert_str}"
        )

    return "\n".join(lignes)


# ──────────────────────────────────────────────
# EXPORT EXCEL
# ──────────────────────────────────────────────

def export_excel(biens: list[dict], resume: str) -> Path:
    """Exporte les résultats scorés dans un fichier Excel."""
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font, Alignment
        from openpyxl.utils import get_column_letter
        from openpyxl.styles import numbers as xl_numbers
    except ImportError:
        raise ImportError("pip install openpyxl")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    path = OUTPUT_DIR / f"resultats_{ts}.xlsx"

    wb = openpyxl.Workbook()

    # ── Feuille 1 : Résultats ──
    ws = wb.active
    ws.title = "Résultats"

    headers = [
        "Score", "Score Visuel", "Verdict Style", "Source", "Titre", "Ville", "Dép", "Type",
        "Surface", "Terrain", "Pièces", "DPE",
        "Prix (€)", "Prix/m²", "Prix/m² marché",
        "Résumé style", "Alertes", "URL"
    ]
    header_fill = PatternFill("solid", fgColor="2C3E50")
    header_font = Font(color="FFFFFF", bold=True)

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    score_fills = {
        "high":   PatternFill("solid", fgColor="D5F5E3"),  # vert
        "medium": PatternFill("solid", fgColor="FEF9E7"),  # jaune
        "low":    PatternFill("solid", fgColor="FADBD8"),  # rouge
    }

    for row, b in enumerate(biens, 2):
        score = b.get("score_total", 0)
        fill = score_fills["high"] if score >= 70 else score_fills["medium"] if score >= 45 else score_fills["low"]

        values = [
            score,
            b.get("score_visuel"),
            b.get("verdict_visuel", ""),
            b.get("source", ""),
            b.get("titre", ""),
            b.get("ville", ""),
            b.get("departement", ""),
            b.get("type_bien", ""),
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
        for col, val in enumerate(values, 1):
            if isinstance(val, (list, dict)):
                val = str(val) if val else ""
            cell = ws.cell(row=row, column=col, value=val)
            cell.fill = fill
            if col == url_col and val:
                cell.hyperlink = str(val)
                cell.value = "Voir l'annonce"
                cell.style = "Hyperlink"  # style natif Excel → change de couleur après clic

    # Largeurs colonnes
    widths = [8, 12, 14, 12, 40, 20, 6, 12, 9, 9, 8, 6, 12, 10, 14, 45, 40, 50]
    for col, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(col)].width = w

    ws.auto_filter.ref = ws.dimensions
    ws.freeze_panes = "A2"

    # ── Feuille 2 : Résumé ──
    ws2 = wb.create_sheet("Résumé")
    ws2["A1"] = "Résumé exécutif"
    ws2["A1"].font = Font(bold=True, size=14)
    ws2["A3"] = resume
    ws2["A3"].alignment = Alignment(wrap_text=True)
    ws2.column_dimensions["A"].width = 100

    wb.save(path)
    print(f"[Analyst] Excel exporté → {path}")
    return path


# ──────────────────────────────────────────────
# POINT D'ENTRÉE
# ──────────────────────────────────────────────

async def run(biens_bruts: list[dict], criteres) -> Path:
    """Pipeline complet : enrichissement DVF → scoring → export Excel."""
    print(f"[Analyst] Scoring de {len(biens_bruts)} biens...")

    criteres_dict = {
        "surface_min": criteres.surface_min,
        "surface_max": criteres.surface_max,
        "terrain_min": criteres.terrain_min,
        "poids_scoring": criteres.poids_scoring,
    }

    # Données marché DVF
    prix_marche = await fetch_prix_marche_dvf(criteres.departements)

    # Scoring
    scored = [score_bien(b, criteres_dict, prix_marche) for b in biens_bruts]

    # Tri par score décroissant
    scored.sort(key=lambda b: b.get("score_total", 0), reverse=True)

    print(f"[Analyst] Top 5 scores : {[b['score_total'] for b in scored[:5]]}")

    # Résumé LLM
    resume = llm_summary(scored[:10])
    print(f"\n--- RÉSUMÉ EXÉCUTIF ---\n{resume}\n")

    # Export
    path = export_excel(scored, resume)
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

"""
workers/analyst.py — Worker 4 : Analyst
Enrichit chaque bien (DVF, match qualitatif NLP, alertes), agrège dans un Excel final.
(Le scoring pondéré a été retiré — à revoir plus tard.)
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

# Département (code INSEE) → nom en toutes lettres. Partagé avec scheduler.py.
DEPT_NOMS = {
    "01": "Ain", "02": "Aisne", "03": "Allier", "04": "Alpes-de-Haute-Provence",
    "05": "Hautes-Alpes", "06": "Alpes-Maritimes", "07": "Ardèche", "08": "Ardennes",
    "09": "Ariège", "10": "Aube", "11": "Aude", "12": "Aveyron",
    "13": "Bouches-du-Rhône", "14": "Calvados", "15": "Cantal", "16": "Charente",
    "17": "Charente-Maritime", "18": "Cher", "19": "Corrèze", "2A": "Corse-du-Sud",
    "2B": "Haute-Corse", "21": "Côte-d'Or", "22": "Côtes-d'Armor", "23": "Creuse",
    "24": "Dordogne", "25": "Doubs", "26": "Drôme", "27": "Eure", "28": "Eure-et-Loir",
    "29": "Finistère", "30": "Gard", "31": "Haute-Garonne", "32": "Gers",
    "33": "Gironde", "34": "Hérault", "35": "Ille-et-Vilaine", "36": "Indre",
    "37": "Indre-et-Loire", "38": "Isère", "39": "Jura", "40": "Landes",
    "41": "Loir-et-Cher", "42": "Loire", "43": "Haute-Loire", "44": "Loire-Atlantique",
    "45": "Loiret", "46": "Lot", "47": "Lot-et-Garonne", "48": "Lozère",
    "49": "Maine-et-Loire", "50": "Manche", "51": "Marne", "52": "Haute-Marne",
    "53": "Mayenne", "54": "Meurthe-et-Moselle", "55": "Meuse", "56": "Morbihan",
    "57": "Moselle", "58": "Nièvre", "59": "Nord", "60": "Oise", "61": "Orne",
    "62": "Pas-de-Calais", "63": "Puy-de-Dôme", "64": "Pyrénées-Atlantiques",
    "65": "Hautes-Pyrénées", "66": "Pyrénées-Orientales", "67": "Bas-Rhin",
    "68": "Haut-Rhin", "69": "Rhône", "70": "Haute-Saône", "71": "Saône-et-Loire",
    "72": "Sarthe", "73": "Savoie", "74": "Haute-Savoie", "75": "Paris",
    "76": "Seine-Maritime", "77": "Seine-et-Marne", "78": "Yvelines",
    "79": "Deux-Sèvres", "80": "Somme", "81": "Tarn", "82": "Tarn-et-Garonne",
    "83": "Var", "84": "Vaucluse", "85": "Vendée", "86": "Vienne",
    "87": "Haute-Vienne", "88": "Vosges", "89": "Yonne", "90": "Territoire de Belfort",
    "91": "Essonne", "92": "Hauts-de-Seine", "93": "Seine-Saint-Denis",
    "94": "Val-de-Marne", "95": "Val-d'Oise", "971": "Guadeloupe", "972": "Martinique",
    "973": "Guyane", "974": "La Réunion", "976": "Mayotte",
}


def dept_nom(code) -> str:
    """Nom du département en toutes lettres ('72' → 'Sarthe'). Repli : le code brut."""
    if code is None:
        return ""
    return DEPT_NOMS.get(str(code).strip().zfill(2), str(code))


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
        "Match qual.", "Source", "Titre", "Ville",
        "Dép", "Département", "Gare", "Bus",
        "Surface", "Terrain", "Pièces", "DPE",
        "Prix (€)", "Prix/m²", "Prix/m² marché",
        "Alertes", "Extrait qual.",
        "Satellite", "Ortho+cadastre", "URL"
    ]
    header_fill = PatternFill("solid", fgColor="2C3E50")
    header_font = Font(color="FFFFFF", bold=True)

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    zebra_fill = PatternFill("solid", fgColor="F2F4F4")    # 1 ligne sur 2

    # Colonnes affichées comme hyperliens : {index_1based: libellé du lien}
    link_labels = {
        headers.index("Satellite") + 1: "Vue satellite",
        headers.index("Ortho+cadastre") + 1: "Ortho + cadastre",
        headers.index("URL") + 1: "Voir l'annonce",
    }
    price_cols = {headers.index(h) + 1 for h in ("Prix (€)", "Prix/m²", "Prix/m² marché", "Terrain")}

    for row, b in enumerate(biens, 2):
        zebra = zebra_fill if row % 2 == 0 else None   # 1 ligne sur 2

        values = [
            b.get("match_qualitatif"),
            b.get("source", ""),
            b.get("titre", ""),
            b.get("ville", ""),
            b.get("departement", ""),
            dept_nom(b.get("departement")),
            (f"{b.get('gare_nom')} ({b.get('gare_distance_km')} km)"
             if b.get("gare_nom") else ""),
            (f"{b.get('bus_nom')} ({b.get('bus_distance_km')} km)"
             if b.get("bus_proche") else ""),
            b.get("surface"),
            b.get("surface_terrain"),
            b.get("pieces"),
            b.get("dpe", ""),
            b.get("prix"),
            b.get("prix_m2_calcule"),
            b.get("prix_m2_marche_dep"),
            " | ".join(b.get("alerte", [])),
            b.get("match_extrait", ""),
            b.get("maps_satellite_url", ""),
            b.get("geoportail_url", ""),
            b.get("url", ""),
        ]
        for col, val in enumerate(values, 1):
            if isinstance(val, (list, dict)):
                val = str(val) if val else ""
            cell = ws.cell(row=row, column=col, value=val)
            # Fond : zébrage 1 ligne sur 2
            if zebra is not None:
                cell.fill = zebra
            # Séparateur de milliers sur les prix
            if col in price_cols and isinstance(val, (int, float)):
                cell.number_format = "#,##0"
            if col in link_labels and val:
                cell.hyperlink = str(val)
                cell.value = link_labels[col]
                cell.style = "Hyperlink"  # style natif Excel → change de couleur après clic

    # Largeurs colonnes
    widths = [8, 12, 40, 20, 6, 18, 24, 22, 9, 9, 8, 6, 12, 10, 14, 45, 40,
              14, 26, 20, 14, 16, 16]
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
        qual_annotate(biens_bruts, desc_qual)

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

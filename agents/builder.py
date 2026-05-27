"""
agents/builder.py — Agent 2 : Builder
Vérifie quels scrapers existent et signale ceux qui manquent.

Sans appel API — les scrapers sont écrits par Claude Code directement
dans scrapers/ lors des sessions de maintenance.

Pour générer un scraper, dis à Claude Code :
  "Génère le scraper pour PAP (RSS, url: pap.fr)"
  "Génère le scraper pour LeBonCoin (scrape_js, anti-bot Cloudflare)"

Interface obligatoire pour chaque scraper :
  async def search(criteres: dict) -> list[dict]

Chaque scraper reçoit :
  criteres = {
    departements, types_bien, surface_min, surface_max,
    prix_max, pieces_min, terrain_min
  }

Chaque scraper retourne une liste de dicts conformes au modèle Bien :
  source, url, id_annonce, titre, type_bien, description,
  departement, ville, code_postal, surface, surface_terrain,
  pieces, chambres, dpe, prix, photos, date_publication, agence
"""
from pathlib import Path
from models import CriteresRecherche

SCRAPERS_DIR = Path(__file__).parent.parent / "scrapers"


async def run(sources: list[dict], criteres: CriteresRecherche) -> list[Path]:
    """
    Vérifie la présence des scrapers pour chaque source active.
    Retourne les paths des scrapers disponibles.
    Signale les scrapers manquants pour action de Claude Code.
    """
    SCRAPERS_DIR.mkdir(exist_ok=True)
    available, missing = [], []

    for source in sources:
        scraper_path = SCRAPERS_DIR / f"{source['id']}.py"
        if scraper_path.exists():
            available.append(scraper_path)
        else:
            missing.append(source)

    print(f"[Builder] {len(available)} scraper(s) disponible(s)")
    for p in available:
        print(f"  ✓ {p.name}")

    if missing:
        print(f"\n[Builder] {len(missing)} scraper(s) manquant(s) :")
        for s in missing:
            print(f"  ✗ {s['id']}.py — {s['nom']} ({s['methode']})")
        print("\n[Builder] Pour générer les scrapers manquants, dis à Claude Code :")
        for s in missing:
            print(f'  "Génère scrapers/{s["id"]}.py pour {s["nom"]} ({s["methode"]}, {s["url_base"]})"')

    _write_init(sources)
    return available


def _write_init(sources: list[dict]):
    """Met à jour scrapers/__init__.py."""
    lines = ['"""Scrapers immo-agent — gérés par Claude Code"""', ""]
    for s in sources:
        path = SCRAPERS_DIR / f"{s['id']}.py"
        status = "✓" if path.exists() else "✗ manquant"
        lines.append(f"# {s['nom']} [{status}]")
    (SCRAPERS_DIR / "__init__.py").write_text("\n".join(lines), encoding="utf-8")


def scraper_exists(source_id: str) -> bool:
    return (SCRAPERS_DIR / f"{source_id}.py").exists()


def list_scrapers() -> list[str]:
    """Liste les scrapers disponibles."""
    return [p.stem for p in SCRAPERS_DIR.glob("*.py") if p.stem != "__init__"]


if __name__ == "__main__":
    import asyncio
    from config_loader import load_criteria, load_sources
    criteres = load_criteria()
    sources = load_sources()
    asyncio.run(run(sources, criteres))

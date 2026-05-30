"""
config_loader.py — Parse criteria.md, source unique de vérité.
"""
import re
import yaml
from pathlib import Path
from models import CriteresRecherche

CONFIG_DIR = Path(__file__).parent / "config"

POIDS_KEYS = [
    "poids_prix", "poids_surface", "poids_terrain",
    "poids_localisation", "poids_etat", "poids_dpe", "poids_style"
]

DEFAULTS = {
    "types":               ["maison", "villa"],
    "surface_min":         80,
    "surface_max":         600,
    "pieces_min":          4,
    "pieces_max":          20,
    "terrain_min":         200,
    "prix_min":            0,
    "prix_max":            600000,
    "dpe_exclus":          ["G"],
    "mots_cles_negatifs":  ["viager", "enchères", "occupé", "indivision"],
    "equipements_requis":  [],
    "gare_obligatoire":    False,
    "gare_rayon_km":       10,
    "geoloc_actif":          True,   # pré-localisation cadastrale (liens + parcelles)
    "geoloc_piscine_ortho":  False,  # détection piscine sur ortho IGN (lourd)
    "geoloc_terrain_tol_pct": 25,    # tolérance écart contenance vs terrain annoncé (%)
    "poids_prix":          25,
    "poids_surface":       20,
    "poids_terrain":       15,
    "poids_localisation":  20,
    "poids_etat":          10,
    "poids_dpe":           10,
    "poids_style":         0,
}


def load_criteria() -> CriteresRecherche:
    md = (CONFIG_DIR / "criteria.md").read_text(encoding="utf-8")

    blocks = re.findall(r"```\s*([\s\S]*?)```", md)

    # Départements — premier bloc
    departements = _parse_departements(blocks[0] if blocks else "")

    # Toutes les valeurs clé: val des blocs suivants
    overrides = {}
    for block in blocks:
        for line in block.strip().splitlines():
            line = line.split("#")[0].strip()
            if ":" in line:
                key, val = line.split(":", 1)
                key = key.strip()
                val = val.strip()
                try:
                    overrides[key] = yaml.safe_load(val)
                except Exception:
                    pass

    def get(key):
        return overrides.get(key, DEFAULTS[key])

    # Pondérations avec normalisation automatique si somme != 100
    poids = {k: get(k) for k in POIDS_KEYS}
    total = sum(poids.values())
    if total > 0 and abs(total - 100) > 1:
        poids = {k: round(v / total * 100, 1) for k, v in poids.items()}

    return CriteresRecherche(
        departements=departements,
        types_bien=get("types"),
        surface_min=get("surface_min"),
        surface_max=get("surface_max"),
        prix_min=get("prix_min"),
        prix_max=get("prix_max"),
        pieces_min=get("pieces_min"),
        pieces_max=get("pieces_max"),
        terrain_min=get("terrain_min"),
        dpe_exclus=get("dpe_exclus"),
        mots_cles_negatifs=get("mots_cles_negatifs"),
        equipements_requis=get("equipements_requis"),
        poids_scoring=poids,
        gare_obligatoire=bool(get("gare_obligatoire")),
        gare_rayon_km=float(get("gare_rayon_km")),
        geoloc_actif=bool(get("geoloc_actif")),
        geoloc_piscine_ortho=bool(get("geoloc_piscine_ortho")),
        geoloc_terrain_tol_pct=float(get("geoloc_terrain_tol_pct")),
    )


def _parse_departements(block: str) -> list[str]:
    deps = []
    for line in block.strip().splitlines():
        code = line.split("#")[0].strip()
        if re.match(r"^\d{2,3}$", code):
            deps.append(code)
    return deps


def load_sources() -> list[dict]:
    sources_path = CONFIG_DIR / "sources.yaml"
    if not sources_path.exists():
        return []
    with open(sources_path) as f:
        data = yaml.safe_load(f)
    return [s for s in data.get("sources", []) if s.get("actif", False)]

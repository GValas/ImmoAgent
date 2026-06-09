"""
config_loader.py — Parse criteria.md, source unique de vérité.
"""
import re
import yaml
from pathlib import Path
from models import CriteresRecherche

CONFIG_DIR = Path(__file__).parent / "config"

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
    "description_qualitative": "",   # texte libre matché sémantiquement à l'annonce
    "photos_min":            0,      # nb minimal de photos exigé (0 = pas de filtre)
}


def load_criteria() -> CriteresRecherche:
    md = (CONFIG_DIR / "criteria.md").read_text(encoding="utf-8")

    blocks = re.findall(r"```\s*([\s\S]*?)```", md)

    # Chaque bloc de code est un fragment YAML (clé: valeur, listes et chaînes
    # multi-lignes, commentaires #). On les charge et on fusionne les dictionnaires.
    overrides = {}
    for block in blocks:
        try:
            data = yaml.safe_load(block)
        except Exception:
            continue
        if isinstance(data, dict):
            overrides.update(data)

    # Départements — clé `departements:` (dans les critères de scraping) ;
    # repli sur le 1ᵉʳ bloc de code si la clé est absente (rétro-compat).
    if "departements" in overrides:
        departements = _coerce_departements(overrides["departements"])
    else:
        departements = _parse_departements(blocks[0] if blocks else "")

    def get(key):
        return overrides.get(key, DEFAULTS[key])

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
        description_qualitative=str(get("description_qualitative") or ""),
        photos_min=int(get("photos_min")),
    )


def _parse_departements(block: str) -> list[str]:
    deps = []
    for line in block.strip().splitlines():
        code = line.split("#")[0].strip()
        if re.match(r"^\d{2,3}$", code):
            deps.append(code)
    return deps


def _coerce_departements(raw) -> list[str]:
    """Normalise une valeur `departements:` (liste YAML) en codes string.

    Gère les entiers (72 → "72"), les codes à un chiffre (6 → "06"),
    les DOM à 3 chiffres (971) et la Corse ("2A"/"2B")."""
    if not isinstance(raw, list):
        raw = [raw]
    deps = []
    for v in raw:
        code = str(v).strip().upper()     # 2b → 2B
        if code.isdigit() and len(code) == 1:
            code = code.zfill(2)          # 6 → "06"
        if re.match(r"^(\d{2,3}|2[AB])$", code):
            deps.append(code)
    return deps


def load_sources() -> list[dict]:
    sources_path = CONFIG_DIR / "sources.yaml"
    if not sources_path.exists():
        return []
    with open(sources_path) as f:
        data = yaml.safe_load(f)
    return [s for s in data.get("sources", []) if s.get("actif", False)]

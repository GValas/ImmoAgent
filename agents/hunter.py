"""
agents/hunter.py — Agent 3 : Hunter
Lance tous les scrapers en parallèle, déduplique les résultats,
et sauvegarde les biens bruts en JSON dans data/raw/.
"""
import asyncio
import hashlib
import importlib.util
import json
from datetime import datetime
from pathlib import Path

from models import Bien, CriteresRecherche
from agents import vision as vision_agent

SCRAPERS_DIR = Path(__file__).parent.parent / "scrapers"
RAW_DIR = Path(__file__).parent.parent / "data" / "raw"


async def run_scraper(source_id: str, criteres: CriteresRecherche) -> list[dict]:
    """Importe dynamiquement et exécute un scraper."""
    scraper_path = SCRAPERS_DIR / f"{source_id}.py"
    if not scraper_path.exists():
        print(f"[Hunter] Scraper {source_id} introuvable — skip")
        return []

    try:
        spec = importlib.util.spec_from_file_location(source_id, scraper_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        criteres_dict = {
            "departements": criteres.departements,
            "types_bien": criteres.types_bien,
            "surface_min": criteres.surface_min,
            "surface_max": criteres.surface_max,
            "prix_max": criteres.prix_max,
            "pieces_min": criteres.pieces_min,
            "terrain_min": criteres.terrain_min,
        }

        results = await module.search(criteres_dict)
        print(f"[Hunter] {source_id} → {len(results)} annonces récupérées")
        return results

    except Exception as e:
        print(f"[Hunter] Erreur sur {source_id} : {e}")
        return []


# Champs à récupérer d'un doublon écarté vers le bien conservé (ne pas perdre les
# coordonnées de Bien'ici quand le doublon gardé vient d'une source sans géoloc).
_MERGE_FIELDS = (
    "latitude", "longitude", "blur_radius_m",
    "surface_terrain", "dpe", "chambres", "pieces", "date_publication",
)


def _merge_duplicate(base: dict, other: dict) -> dict:
    """Complète `base` (in place) avec les champs utiles de `other` qui lui manquent."""
    for k in _MERGE_FIELDS:
        if not base.get(k) and other.get(k):
            base[k] = other[k]
    if not base.get("photos") and other.get("photos"):
        base["photos"] = other["photos"]
    if other.get("has_pool"):            # une piscine signalée par une source suffit
        base["has_pool"] = True
    return base


def deduplicate(biens: list[dict]) -> list[dict]:
    """
    Déduplique par hash (prix + surface + ville).
    Garde la version la plus complète, mais récupère les champs manquants (surtout
    les coordonnées) depuis le doublon écarté — Bien'ici aggrège IAD/SAFTI/ERA…, donc
    sans cette fusion ses coordonnées seraient perdues au profit du doublon direct.
    """
    seen: dict[str, dict] = {}
    for bien in biens:
        prix = bien.get("prix", "")
        surface = bien.get("surface", "")
        ville = str(bien.get("ville", "")).lower().strip()
        key = hashlib.md5(f"{prix}-{surface}-{ville}".encode()).hexdigest()

        if key not in seen:
            seen[key] = dict(bien)
            continue

        existing = seen[key]
        # Le plus complet devient la base ; l'autre lui cède ses champs manquants.
        if sum(1 for v in bien.values() if v) > sum(1 for v in existing.values() if v):
            base, extra = dict(bien), existing
        else:
            base, extra = existing, bien
        seen[key] = _merge_duplicate(base, extra)

    return list(seen.values())


def filter_biens(biens: list[dict], criteres: CriteresRecherche) -> list[dict]:
    """Applique les filtres d'exclusion durs."""
    filtered = []
    for b in biens:
        desc = ((b.get("description") or "") + " " + (b.get("titre") or "")).lower()

        # Mots-clés négatifs
        if any(mot.lower() in desc for mot in criteres.mots_cles_negatifs):
            continue

        # DPE exclu
        dpe = b.get("dpe", "")
        if dpe and dpe.upper() in [d.upper() for d in criteres.dpe_exclus]:
            continue

        # Prix min / max
        prix = b.get("prix")
        if prix and prix > criteres.prix_max:
            continue
        if prix and getattr(criteres, 'prix_min', 0) and prix < criteres.prix_min:
            continue

        # Surface min
        surface = b.get("surface")
        if surface and surface < criteres.surface_min:
            continue

        # Pièces max
        pieces = b.get("pieces")
        if pieces and getattr(criteres, "pieces_max", 0) and pieces > criteres.pieces_max:
            continue

        filtered.append(b)

    return filtered


_PISCINE_NEG_CTX = [
    "possibilit", "à créer", "a creer", "à construire", "a construire",
    "emplacement", "municipal", "intercommunal", "à proximit", "a proximit",
    "prévu", "prevu", "futur", "projet", "potentiel", "envisa",
    "syndicat", "piscine de la", "piscine du",
]


def _piscine_owned_in_text(texte: str) -> bool:
    """
    Retourne True si le texte mentionne une piscine appartenant au bien
    (pas une piscine municipale, pas une possibilité future, etc.).
    """
    import re
    texte = texte.lower()
    for m in re.finditer(r"\bpiscine\b", texte):
        window = texte[max(0, m.start() - 90): m.end() + 90]
        if not any(neg in window for neg in _PISCINE_NEG_CTX):
            return True
    return False


def filter_equipements_post_vision(biens: list[dict], criteres: CriteresRecherche) -> list[dict]:
    """
    Exclusion dure sur équipements requis, après scoring visuel CLIP.
    Pour 'piscine' : exclut si non mentionné en tant que bien du bien (texte)
                     ET non détecté visuellement par CLIP.
    Pour les autres équipements : exclut si absent du texte.
    """
    equipements = getattr(criteres, "equipements_requis", [])
    if not equipements:
        return biens

    kept, excluded = [], 0
    for b in biens:
        texte = ((b.get("titre") or "") + " " + (b.get("description") or "")).lower()

        exclure = False
        for e in equipements:
            if e.lower() == "piscine":
                has_api = b.get("has_pool", False)
                has_text = _piscine_owned_in_text(texte)
                has_visual = b.get("piscine_visuelle", False)
                if not (has_api or (has_text and has_visual)):
                    exclure = True
                    break
            else:
                if e.lower() not in texte:
                    exclure = True
                    break

        if exclure:
            excluded += 1
        else:
            kept.append(b)

    print(f"[Hunter] Filtre piscine : {len(kept)} conservés | {excluded} exclus")
    return kept


async def run(sources: list[dict], criteres: CriteresRecherche) -> list[dict]:
    """
    Lance tous les scrapers en parallèle, déduplique, filtre,
    sauvegarde et retourne les biens bruts.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    # Lancement parallèle
    tasks = [run_scraper(s["id"], criteres) for s in sources]
    results_per_source = await asyncio.gather(*tasks)

    # Flat list
    all_biens = []
    for biens in results_per_source:
        all_biens.extend(biens)

    print(f"\n[Hunter] Total brut : {len(all_biens)} annonces")

    # Déduplication
    deduped = deduplicate(all_biens)
    print(f"[Hunter] Après déduplication : {len(deduped)} annonces")

    # Filtrage dur (prix, surface, DPE, mots-clés négatifs)
    filtered = filter_biens(deduped, criteres)
    print(f"[Hunter] Après filtrage : {len(filtered)} annonces")

    # Filtre gare SNCF voyageurs à proximité (avant la vision pour épargner le CLIP)
    if getattr(criteres, "gare_obligatoire", False):
        from scrapers.gares import filter_biens_gare
        filtered = await filter_biens_gare(filtered, criteres.gare_rayon_km)
        print(f"[Hunter] Après filtre gare : {len(filtered)} annonces")

    # Sauvegarde pré-vision (permet --only-vision sans re-scraper)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    prevision_path = RAW_DIR / f"biens_prevision_{ts}.json"
    prevision_path.write_text(json.dumps(filtered, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[Hunter] Pré-vision sauvegardé → {prevision_path}")

    # Filtre visuel (style CLIP + détection piscine)
    filtered = await vision_agent.run(filtered)
    print(f"[Hunter] Après filtre visuel : {len(filtered)} annonces")

    # Exclusion dure équipements requis (piscine texte + CLIP)
    filtered = filter_equipements_post_vision(filtered, criteres)
    print(f"[Hunter] Après filtre équipements : {len(filtered)} annonces\n")

    # Pré-localisation cadastrale (liens satellite + parcelles candidates par surface
    # terrain, et optionnellement détection piscine sur orthophoto IGN).
    if getattr(criteres, "geoloc_actif", True):
        from scrapers.geolocate import annotate_biens as geo_annotate
        filtered = await geo_annotate(filtered, criteres)

    # Sauvegarde raw finale
    raw_path = RAW_DIR / f"biens_raw_{ts}.json"
    raw_path.write_text(json.dumps(filtered, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[Hunter] Sauvegardé → {raw_path}")

    return filtered


if __name__ == "__main__":
    from config_loader import load_criteria, load_sources
    criteres = load_criteria()
    sources = load_sources()
    asyncio.run(run(sources, criteres))

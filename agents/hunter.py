"""
agents/hunter.py — Agent 3 : Hunter
Lance tous les scrapers en parallèle, déduplique les résultats,
et sauvegarde les biens bruts en JSON dans data/raw/.
"""
import asyncio
import hashlib
import importlib.util
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from PIL import Image

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


# ──────────────────────────────────────────────
# DÉDUPLICATION INTER-SOURCES PAR EMPREINTE PHOTO
# ──────────────────────────────────────────────
#
# Problème : un même bien publié sur deux sites (URL/prix/ville différents) échappe
# à `deduplicate()` (hash prix+surface+ville) — ex. prix net vendeur vs FAI, ville
# mal saisie (notaires Romorantin-41 vs proprietes_privees "Châteauroux"-36).
# Seules les PHOTOS sont fiables : même bien ⇒ mêmes photos.
#
# Conception (coût borné) :
#   1. On NE traite QUE les survivants post-filtres (gare/vision) — petit ensemble,
#      photos déjà jugées pertinentes — pas les ~12000 biens bruts.
#   2. Clé de BLOCAGE bon marché = surface arrondie à l'entier → on ne compare que
#      les biens de surface quasi identique. (Le prix n'entre PAS dans la clé : net
#      vs FAI peut différer de >10%.) Les groupes de taille 1 sont ignorés.
#   3. Empreinte = dHash 64 bits (resize 9×8 grayscale, comparaison des pixels
#      horizontaux adjacents) de la 1ʳᵉ photo, via Pillow uniquement.
#   4. Doublon ssi distance de Hamming ≤ HAMMING_THRESHOLD ET surface ±SURFACE_TOL_PCT
#      ET sources différentes.
#   5. Fusion : on garde le bien au prix le plus bas (= net vendeur, le plus utile),
#      union des champs manquants via `_merge_duplicate`.
# Non-fatal : toute photo absente / illisible ⇒ le bien est simplement laissé tel quel.

# Seuil de Hamming sur 64 bits. dHash sur une même image ré-encodée/redimensionnée
# par deux sites diffère typiquement de 0–6 bits ; des images réellement distinctes
# dépassent largement 12. 8 est un compromis prudent (peu de faux positifs).
HAMMING_THRESHOLD = 8
# Tolérance surface entre deux candidats fusionnés (la surface habitable annoncée
# est en général identique d'une source à l'autre, contrairement au prix).
SURFACE_TOL_PCT = 2.0


def dhash(pil_image: Image.Image, hash_size: int = 8) -> int:
    """
    Perceptual hash (dHash) d'une image → entier de hash_size² bits (64 par défaut).
    Resize en (hash_size+1)×hash_size niveaux de gris, puis compare chaque pixel à
    son voisin de droite. Robuste au ré-encodage / redimensionnement / léger recadrage.
    """
    img = pil_image.convert("L").resize(
        (hash_size + 1, hash_size), Image.Resampling.LANCZOS
    )
    px = list(img.getdata())
    bits = 0
    bit_index = 0
    for row in range(hash_size):
        row_off = row * (hash_size + 1)
        for col in range(hash_size):
            left = px[row_off + col]
            right = px[row_off + col + 1]
            if left > right:
                bits |= (1 << bit_index)
            bit_index += 1
    return bits


def hamming_distance(a: int, b: int) -> int:
    """Nombre de bits différents entre deux empreintes (popcount du XOR)."""
    return (a ^ b).bit_count()


def _block_key_surface(bien: dict) -> Optional[int]:
    """Clé de blocage = surface habitable arrondie. None si pas de surface."""
    surface = bien.get("surface")
    if not surface:
        return None
    try:
        return int(round(float(surface)))
    except (TypeError, ValueError):
        return None


def _surfaces_compatibles(a: dict, b: dict, tol_pct: float = SURFACE_TOL_PCT) -> bool:
    sa, sb = a.get("surface"), b.get("surface")
    if not sa or not sb:
        return False
    try:
        sa, sb = float(sa), float(sb)
    except (TypeError, ValueError):
        return False
    if max(sa, sb) == 0:
        return False
    return abs(sa - sb) / max(sa, sb) * 100.0 <= tol_pct


async def _first_photo_hash(bien: dict, session: httpx.AsyncClient) -> Optional[int]:
    """Télécharge la 1ʳᵉ photo d'un bien et retourne son dHash. None si indisponible."""
    urls = bien.get("photos") or []
    if not urls:
        return None
    try:
        r = await session.get(urls[0], timeout=10, follow_redirects=True)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        return dhash(img)
    except Exception:
        return None


async def deduplicate_by_photo(
    biens: list[dict],
    concurrency: int = 8,
) -> list[dict]:
    """
    Déduplication inter-sources par empreinte photo (dHash de la 1ʳᵉ photo).

    À lancer sur un petit ensemble (survivants post-filtres). Stratégie de blocage
    par surface arrondie ⇒ on ne calcule l'empreinte QUE dans les groupes de taille
    ≥2 mélangeant des sources différentes : aucun téléchargement hors candidats.

    Fusionne chaque cluster de doublons en un seul bien (prix le plus bas = net
    vendeur conservé, union des champs). Non-fatal : photo absente/illisible ⇒ bien
    laissé intact. Retourne la liste dédupliquée.
    """
    # 1. Blocage par surface : on ne garde comme candidats que les biens d'un groupe
    #    contenant au moins 2 sources distinctes.
    groups: dict[int, list[int]] = {}
    for idx, b in enumerate(biens):
        k = _block_key_surface(b)
        if k is None:
            continue
        groups.setdefault(k, []).append(idx)

    candidate_idx: set[int] = set()
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        sources = {biens[i].get("source") for i in idxs}
        if len(sources) < 2:
            continue
        candidate_idx.update(idxs)

    if not candidate_idx:
        return biens

    # 2. Empreinte photo uniquement pour les candidats (petits groupes).
    semaphore = asyncio.Semaphore(concurrency)

    async def hash_one(i: int, session: httpx.AsyncClient):
        async with semaphore:
            return i, await _first_photo_hash(biens[i], session)

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; immo-agent/1.0)"},
        timeout=15,
    ) as session:
        hashed = await asyncio.gather(
            *[hash_one(i, session) for i in candidate_idx]
        )
    hashes: dict[int, int] = {i: h for i, h in hashed if h is not None}

    # 3. Clustering : dans chaque groupe de surface, relie les biens dont les
    #    empreintes sont proches (Hamming ≤ seuil), surface compatible, sources ≠.
    parent = {i: i for i in hashes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    for idxs in groups.values():
        hs = [i for i in idxs if i in hashes]
        for a in range(len(hs)):
            for bb in range(a + 1, len(hs)):
                i, j = hs[a], hs[bb]
                if biens[i].get("source") == biens[j].get("source"):
                    continue
                if not _surfaces_compatibles(biens[i], biens[j]):
                    continue
                if hamming_distance(hashes[i], hashes[j]) <= HAMMING_THRESHOLD:
                    union(i, j)

    # 4. Fusion des clusters (>1 membre). On garde le prix le plus bas.
    clusters: dict[int, list[int]] = {}
    for i in hashes:
        clusters.setdefault(find(i), []).append(i)

    drop: set[int] = set()
    merged_count = 0
    for members in clusters.values():
        if len(members) < 2:
            continue
        # Bien conservé = prix le plus bas (net vendeur). À prix égal/absent, le plus complet.
        def _sort_key(i):
            prix = biens[i].get("prix")
            prix_rank = prix if prix else float("inf")
            completeness = -sum(1 for v in biens[i].values() if v)
            return (prix_rank, completeness)

        members_sorted = sorted(members, key=_sort_key)
        keeper = members_sorted[0]
        for other in members_sorted[1:]:
            _merge_duplicate(biens[keeper], biens[other])
            biens[keeper].setdefault("alerte", []).append(
                f"📸 Doublon photo fusionné depuis {biens[other].get('source')} "
                f"({biens[other].get('prix')}€ / {biens[other].get('ville')})"
            )
            drop.add(other)
            merged_count += 1

    if merged_count:
        print(f"[Dedup] {merged_count} doublon(s) inter-sources fusionné(s) (empreinte photo)")

    return [b for idx, b in enumerate(biens) if idx not in drop]


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

    # Déduplication inter-sources par empreinte photo (sur les survivants, peu nombreux —
    # ne télécharge la 1ʳᵉ photo que des candidats regroupés par surface). Attrape un même
    # bien publié sur deux sites avec prix/ville différents, que le hash exact rate.
    before = len(filtered)
    filtered = await deduplicate_by_photo(filtered)
    if len(filtered) != before:
        print(f"[Hunter] Après dédup photo : {len(filtered)} annonces\n")

    # Annotation bus (informatif, NON éliminatoire) — sur les survivants pour
    # limiter les requêtes Overpass. Non-fatal : n'élimine ni ne plante rien.
    if getattr(criteres, "bus_actif", True):
        from scrapers.bus import annotate_biens as bus_annotate
        filtered = await bus_annotate(filtered, criteres.bus_rayon_km)

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

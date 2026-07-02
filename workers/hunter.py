"""
workers/hunter.py — Worker 3 : Hunter
Lance tous les scrapers en parallèle, déduplique les résultats,
et sauvegarde les biens bruts en JSON dans data/raw/.
"""
import asyncio
import contextlib
import importlib.util
import io
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from PIL import Image

from core.dedup import dedup_hash

# Filtres a posteriori centralisés dans core.filters (réexportés ici pour
# compatibilité : orchestrator/scheduler référencent encore hunter.filter_biens…).
from core.filters import (  # noqa: F401
    extract_terrain_from_text,
    filter_biens,
    filter_mots_cles,
    filter_photos_min,
    refilter_dpe,
    refilter_terrain_from_text,
)
from core.state_io import atomic_write_json
from models import CriteresRecherche

SCRAPERS_DIR = Path(__file__).parent.parent / "scrapers"
RAW_DIR = Path(__file__).parent.parent / "data" / "raw"

# Rayon (km) du flag "gare proche" pour l'annotation gare (non éliminatoire).
GARE_RAYON_KM = 20.0

# Rayon (km) du flag "bus proche" — court : un arrêt de bus utile est proche
# (annotation informative, non éliminatoire).
BUS_RAYON_KM = 2.0

# Bornes du fan-out scrapers. Sans elles, les ~300 scrapers partaient dans un
# seul gather : ~300 pools de connexions simultanés, ~12 Chromium concurrents
# (2-4 Go de RAM), et UN scraper suspendu bloquait le run entier pour toujours.
SCRAPER_CONCURRENCY = int(os.environ.get("SCRAPER_CONCURRENCY", "30"))
SCRAPER_TIMEOUT_S = float(os.environ.get("SCRAPER_TIMEOUT_S", "600"))
# Les scrapers Playwright lancent chacun un Chromium → plafond dédié, bien plus bas.
PLAYWRIGHT_CONCURRENCY = int(os.environ.get("PLAYWRIGHT_CONCURRENCY", "2"))


def _uses_playwright(source_id: str) -> bool:
    """Détecte (une fois, à froid) si un scraper lance Playwright/Chromium."""
    try:
        src = (SCRAPERS_DIR / f"{source_id}.py").read_text(encoding="utf-8", errors="ignore")
        return "playwright" in src
    except Exception:
        return False


def _criteres_to_dict(criteres: CriteresRecherche) -> dict:
    """Dict passé à `scraper.search`. Inclut `prix_min` (lu par ~291 scrapers mais
    jadis jamais transmis → valait toujours 0 en prod)."""
    return {
        "departements": criteres.departements,
        "types_bien": criteres.types_bien,
        "surface_min": criteres.surface_min,
        "surface_max": criteres.surface_max,
        "prix_min": getattr(criteres, "prix_min", 0),
        "prix_max": criteres.prix_max,
        "pieces_min": criteres.pieces_min,
        "terrain_min": criteres.terrain_min,
    }


async def run_scraper(source_id: str, criteres: CriteresRecherche) -> Optional[list[dict]]:
    """Importe dynamiquement et exécute un scraper.

    Retourne la liste d'annonces (éventuellement vide = 0 résultat réel), ou `None`
    si le scraper a planté/est introuvable — ce qui permet à `run()` de distinguer
    « scraper cassé » de « scraper OK mais 0 annonce »."""
    scraper_path = SCRAPERS_DIR / f"{source_id}.py"
    if not scraper_path.exists():
        print(f"[Hunter] Scraper {source_id} introuvable — skip")
        return None

    try:
        spec = importlib.util.spec_from_file_location(source_id, scraper_path)
        module = importlib.util.module_from_spec(spec)
        # exec_module est SYNCHRONE : exécuté dans un thread pour qu'un import
        # lent (I/O au chargement) ne gèle pas les ~30 scrapers concurrents.
        await asyncio.to_thread(spec.loader.exec_module, module)

        results = await module.search(_criteres_to_dict(criteres))
        print(f"[Hunter] {source_id} → {len(results)} annonces récupérées")
        return results

    except Exception as e:
        print(f"[Hunter] ⚠️  Erreur sur {source_id} (scraper cassé ?) : {e}")
        return None


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
        key = dedup_hash(bien)

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
#   1. On NE traite QUE les survivants post-filtres (gare/critères) — petit ensemble,
#      pas les ~12000 biens bruts.
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


# Nombre de snapshots data/raw/ conservés (les plus anciens sont purgés à chaque run).
RAW_RETENTION = 48
HEALTH_FILE = RAW_DIR.parent / "scraper_health.json"


def _prune_old_raw(keep: int = RAW_RETENTION) -> None:
    """Supprime les plus vieux snapshots data/raw/ pour borner la croissance disque
    (~1 Mo/run). Conserve les `keep` plus récents. Best-effort (ne lève jamais)."""
    try:
        files = sorted(RAW_DIR.glob("biens_raw_*.json"))
        for old in files[:-keep] if keep else []:
            old.unlink(missing_ok=True)
    except Exception as e:
        print(f"[Hunter] Purge data/raw ignorée ({e})")


def _save_scraper_health(sources: list[dict], results: list) -> None:
    """Persiste, par source, le nb d'annonces du run et le nb de runs consécutifs à
    0 (ou en échec) — pour repérer les scrapers morts à passer `actif: false`.
    Best-effort (ne lève jamais)."""
    try:
        # Lecture de l'état précédent isolée : un fichier corrompu (kill mi-écriture
        # avant le passage à l'écriture atomique) repartait autrement en exception et
        # désactivait le suivi santé POUR TOUJOURS (le fichier n'était jamais réécrit).
        prev = {}
        try:
            if HEALTH_FILE.exists():
                prev = json.loads(HEALTH_FILE.read_text(encoding="utf-8")).get("sources", {})
        except Exception:
            print("[Hunter] scraper_health.json illisible — compteurs repartis de zéro")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        health = {}
        for s, res in zip(sources, results):
            sid = s["id"]
            if not (SCRAPERS_DIR / f"{sid}.py").exists():
                # Scraper jamais généré : marqué distinctement, PAS compté comme
                # « muet » (sinon il pollue muets_5runs avec du « n'existe pas »).
                health[sid] = {"last_count": "absent", "zero_streak": 0, "last_run": ts}
                continue
            count = -1 if res is None else len(res)            # -1 = scraper en échec
            streak = prev.get(sid, {}).get("zero_streak", 0)
            streak = streak + 1 if count <= 0 else 0
            health[sid] = {"last_count": count, "zero_streak": streak, "last_run": ts}
        dead = sorted(sid for sid, h in health.items() if h["zero_streak"] >= 5)
        atomic_write_json(HEALTH_FILE,
                          {"updated": ts, "muets_5runs": dead, "sources": health},
                          ensure_ascii=False, indent=2)
        if dead:
            print(f"[Hunter] ⚠️  {len(dead)} scraper(s) muet(s) depuis ≥5 runs "
                  f"(candidats actif:false) : {', '.join(dead[:10])}"
                  + (" …" if len(dead) > 10 else ""))
    except Exception as e:
        print(f"[Hunter] Suivi santé scrapers ignoré ({e})")


async def run(
    sources: list[dict],
    criteres: CriteresRecherche,
    seen: Optional[dict] = None,
) -> list[dict]:
    """
    Lance tous les scrapers en parallèle (concurrence bornée + timeout par
    scraper), déduplique, filtre, enrichit, sauvegarde et retourne les biens.

    `seen` (optionnel, passé par le scheduler) : dict {hash: date ISO de dernière
    vue}. Les biens déjà vus sont écartés AVANT l'enrichissement page détail
    (galerie/DPE/photos/bus/geoloc) — en régime de croisière ~95% des survivants
    sont déjà connus : les enrichir pour les jeter ensuite était l'essentiel du
    trafic réseau du cycle. Leur timestamp est rafraîchi in place dans `seen`
    (l'annonce est toujours en ligne). Sans `seen` (orchestrator), comportement
    complet inchangé.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    # Lancement parallèle, borné : plafond global + plafond Playwright (Chromium)
    # + timeout par scraper (un scraper suspendu ne bloque plus le run entier ;
    # timeout → None, compté comme échec par le suivi santé).
    scraper_sem = asyncio.Semaphore(SCRAPER_CONCURRENCY)
    playwright_sem = asyncio.Semaphore(PLAYWRIGHT_CONCURRENCY)

    async def _bounded_run(source_id: str) -> Optional[list[dict]]:
        gate = playwright_sem if _uses_playwright(source_id) else contextlib.nullcontext()
        async with scraper_sem, gate:
            try:
                return await asyncio.wait_for(
                    run_scraper(source_id, criteres), SCRAPER_TIMEOUT_S)
            except asyncio.TimeoutError:
                print(f"[Hunter] ⏱  {source_id} : timeout {SCRAPER_TIMEOUT_S:.0f}s — abandonné")
                return None

    results_per_source = await asyncio.gather(*[_bounded_run(s["id"]) for s in sources])

    # Flat list. run_scraper renvoie None si le scraper a planté (vs [] = 0 annonce
    # réelle) → on compte les échecs séparément pour repérer les scrapers à réparer.
    all_biens = []
    n_crashed = n_empty = 0
    for src, biens in zip(sources, results_per_source):
        if biens is None:
            n_crashed += 1
        elif not biens:
            n_empty += 1
        else:
            all_biens.extend(biens)

    print(f"\n[Hunter] Total brut : {len(all_biens)} annonces "
          f"({len(sources)} sources, {n_empty} à 0 résultat, {n_crashed} en échec)")
    _save_scraper_health(sources, results_per_source)

    # Déduplication
    deduped = deduplicate(all_biens)
    print(f"[Hunter] Après déduplication : {len(deduped)} annonces")

    # Filtrage dur (prix, surface, DPE)
    filtered = filter_biens(deduped, criteres)
    print(f"[Hunter] Après filtrage : {len(filtered)} annonces")

    # Écarter les biens DÉJÀ VUS avant tout enrichissement (mode scheduler).
    # Leur timestamp est rafraîchi : l'annonce est toujours en ligne.
    if seen is not None:
        now_iso = datetime.now().isoformat(timespec="seconds")
        fresh = []
        for b in filtered:
            h = dedup_hash(b)
            if h in seen:
                seen[h] = now_iso
            else:
                fresh.append(b)
        if len(fresh) != len(filtered):
            print(f"[Hunter] Déjà vus : {len(filtered) - len(fresh)} bien(s) écarté(s) "
                  f"avant enrichissement (biens_vus.json)")
        filtered = fresh

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    raw_path = RAW_DIR / f"biens_raw_{ts}.json"

    # Sauvegarde raw PRÉCOCE (pré-enrichissement) : si une annotation/enrichissement
    # plante malgré les garde-fous, le scrape de ~300 sources n'est pas perdu — le
    # fichier est réécrit enrichi en fin de run.
    if filtered:
        atomic_write_json(raw_path, filtered, ensure_ascii=False, indent=2, default=str)

    # Enrichissement galerie : récupère la galerie COMPLÈTE depuis la page détail
    # des survivants (la plupart des scrapers ne captent que 0-1 photo en vue liste).
    # Fait ici, sur ~les survivants, pour ne pas visiter 12000 pages détail.
    from collections import defaultdict as _dd
    from urllib.parse import urlparse as _up

    import httpx as _httpx

    from scrapers.gallery import fetch_gallery, reset_breaker
    reset_breaker()   # coupe-circuit par domaine, neuf à chaque passe
    async with _httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; immo-agent/1.0)"},
        follow_redirects=True, timeout=20,
    ) as _gc:
        _sem = asyncio.Semaphore(24)               # plafond global
        _dom_sem = _dd(lambda: asyncio.Semaphore(3))  # max 3 requêtes simultanées / domaine
        # (le coupe-circuit 429 de gallery.py protège les sites sensibles)
        async def _enrich(b):                       # évite le 429 (ex. century21)
            dom = _up(str(b.get("url") or "")).netloc
            async with _sem, _dom_sem[dom]:
                try:
                    g = await fetch_gallery(b, _gc)
                    if g and len(g) > len(b.get("photos") or []):
                        b["photos"] = g
                except Exception:
                    pass
        await asyncio.gather(*[_enrich(b) for b in filtered])
    import statistics as _st
    _counts = [len(b.get("photos") or []) for b in filtered]
    _med = int(_st.median(_counts)) if _counts else 0
    print(f"[Hunter] Galerie enrichie : médiane {_med} photos/bien "
          f"({sum(1 for c in _counts if c >= 3)}/{len(filtered)} ont ≥3 photos)")
    _ndpe = sum(1 for b in filtered if b.get("dpe"))
    print(f"[Hunter] DPE capté (post-détail) : {_ndpe}/{len(filtered)} biens")

    # Re-filtres post-détail — séquence PARTAGÉE de core.filters (le DPE, le terrain
    # et la description complète ne sont fiables qu'APRÈS la page détail).
    before = len(filtered)
    filtered = refilter_dpe(filtered, criteres)
    if before - len(filtered):
        _dpe_excl = [str(d).upper() for d in getattr(criteres, "dpe_exclus", [])]
        print(f"[Hunter] Re-filtre DPE ({'/'.join(_dpe_excl)}) : "
              f"{before - len(filtered)} bien(s) écarté(s)")

    _tmin = getattr(criteres, "terrain_min", 0)
    if _tmin:
        _flag_avant = sum(1 for b in filtered if b.get("terrain_estime_texte"))
        before = len(filtered)
        filtered = refilter_terrain_from_text(filtered, criteres)
        _enr = sum(1 for b in filtered if b.get("terrain_estime_texte")) - _flag_avant
        if _enr or before - len(filtered):
            print(f"[Hunter] Terrain post-détail : {max(_enr, 0)} extrait(s) du texte ; "
                  f"re-filtre terrain_min({_tmin}) : {before - len(filtered)} bien(s) écarté(s)")

    # Filtre mots-clés (obligatoires/interdits) sur l'annonce COMPLÈTE.
    before = len(filtered)
    filtered = filter_mots_cles(filtered, criteres)
    if before - len(filtered):
        _mo = getattr(criteres, "mots_obligatoires", []) or []
        _mi = getattr(criteres, "mots_interdits", []) or []
        print(f"[Hunter] Filtre mots-clés (oblig={_mo} interdits={_mi}) : "
              f"{len(filtered)}/{before} conservés")

    # photos_min : exclure les annonces trop pauvres (APRÈS enrichissement galerie)
    pmin = getattr(criteres, "photos_min", 0)
    if pmin:
        before = len(filtered)
        filtered = filter_photos_min(filtered, criteres)
        print(f"[Hunter] Filtre photos_min({pmin}) : {len(filtered)}/{before} conservés")

    # Déduplication inter-sources par empreinte photo (sur les survivants, peu nombreux —
    # ne télécharge la 1ʳᵉ photo que des candidats regroupés par surface). Attrape un même
    # bien publié sur deux sites avec prix/ville différents, que le hash exact rate.
    before = len(filtered)
    try:
        filtered = await deduplicate_by_photo(filtered)
    except Exception as e:
        print(f"[Hunter] ⚠️  Dédup photo ignorée ({type(e).__name__}: {e})")
    if len(filtered) != before:
        print(f"[Hunter] Après dédup photo : {len(filtered)} annonces\n")

    # Annotations (toutes NON éliminatoires, toutes best-effort : une panne d'une
    # source open-data — SNCF, Overpass, geo.api — ne doit JAMAIS détruire le run).
    # La gare est annotée ICI, sur les survivants finaux (elle tournait avant les
    # re-filtres → géocodage gaspillé sur des biens ensuite écartés).
    from scrapers.gares import annotate_biens as gare_annotate
    try:
        filtered = await gare_annotate(filtered, GARE_RAYON_KM)
    except Exception as e:
        print(f"[Hunter] ⚠️  Annotation gare ignorée ({type(e).__name__}: {e})")

    from scrapers.bus import annotate_biens as bus_annotate
    try:
        filtered = await bus_annotate(filtered, BUS_RAYON_KM)
    except Exception as e:
        print(f"[Hunter] ⚠️  Annotation bus ignorée ({type(e).__name__}: {e})")

    # Pré-localisation (liens satellite + ortho/cadastre), toujours active.
    from scrapers.geolocate import annotate_biens as geo_annotate
    try:
        filtered = await geo_annotate(filtered)
    except Exception as e:
        print(f"[Hunter] ⚠️  Pré-localisation ignorée ({type(e).__name__}: {e})")

    # Nettoyage des clés de travail internes (jamais persistées).
    for b in filtered:
        b.pop("_geo_candidates", None)

    # Sauvegarde raw finale (écrase la version pré-enrichissement du début de run)
    atomic_write_json(raw_path, filtered, ensure_ascii=False, indent=2, default=str)
    print(f"[Hunter] Sauvegardé → {raw_path}")
    _prune_old_raw()   # borne la croissance de data/raw/

    return filtered


if __name__ == "__main__":
    from config_loader import load_criteria, load_sources
    criteres = load_criteria()
    sources = load_sources()
    asyncio.run(run(sources, criteres))

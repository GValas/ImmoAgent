"""
agents/vision.py — Agent Vision (CLIP local)

Filtre les biens par PRÉSENCE D'ÉLÉMENTS indésirables dans leurs photos
(piscine hors-sol, gazon artificiel, absence d'arbres…), définis dans
config/elements.yaml. Détecte aussi la présence d'une piscine (informatif).

Fonctionnement :
1. Au démarrage : charge CLIP en local (ViT-B/32, ~340 MB, téléchargé une fois)
2. Pour chaque bien : télécharge ses photos, encode (CLIP image)
3. Pour chaque élément : classification binaire CONTRASTIVE (présent vs absent),
   MAX sur les photos → écarte (mode exclusion) ou alerte (mode alerte)

Aucune donnée ne quitte ta machine. Gratuit, illimité.

Éléments indésirables : config/elements.yaml
  détecteur CLIP zero-shot contrastif, par élément, avec seuil et mode
  (exclusion | alerte). Prompts en anglais (CLIP est entraîné en anglais).
  Calibrer un élément : python agents/vision.py --calibrer <nom_element>
"""

import asyncio
from pathlib import Path
from typing import Optional
import numpy as np
import httpx
import yaml
from PIL import Image
import io

# Chargement lazy de CLIP pour ne pas bloquer l'import si non installé
_clip_model = None
_clip_preprocess = None
_clip_device = None
_pool_text_embeddings = None  # np.ndarray (P, 512) — requêtes texte piscine
_elements_cfg = None          # list[dict] — éléments chargés depuis elements.yaml
_element_text_emb = {}        # nom_element -> np.ndarray (npos+nneg, 512), cache session


ELEMENTS_YAML = Path(__file__).parent.parent / "config" / "elements.yaml"

MAX_PHOTOS_PAR_BIEN = 5     # photos utilisées (legacy : galerie courte)
MAX_PHOTOS_PISCINE = 15    # photos téléchargées par bien (détection sur toute la galerie)

# Détection piscine via CLIP text-image matching
POOL_PROMPTS = [
    "swimming pool",
    "backyard with a rectangular swimming pool",
    "outdoor swimming pool in garden",
    "piscine rectangulaire dans le jardin",
    "maison avec piscine exterieure",
]
POOL_THRESHOLD = 0.28  # similarité cosinus CLIP image↔texte (0.22 génère 90%+ de faux positifs)


# ──────────────────────────────────────────────
# INIT CLIP
# ──────────────────────────────────────────────

def _init_clip():
    """Charge le modèle CLIP une seule fois (lazy)."""
    global _clip_model, _clip_preprocess, _clip_device
    if _clip_model is not None:
        return

    try:
        import os
        import clip
        import torch
        cuda_ok = torch.cuda.is_available()
        # IMMO_FORCE_GPU=1 : exige un GPU et échoue bruyamment plutôt que de
        # retomber silencieusement sur CPU (10× plus lent). Activé en prod (Docker).
        if os.environ.get("IMMO_FORCE_GPU") == "1" and not cuda_ok:
            raise RuntimeError(
                "IMMO_FORCE_GPU=1 mais torch ne voit aucun GPU CUDA. "
                "Vérifie : torch CUDA installé, --gpus all / nvidia-container-toolkit, "
                "et le driver NVIDIA sur l'hôte."
            )
        _clip_device = "cuda" if cuda_ok else "cpu"
        _clip_model, _clip_preprocess = clip.load("ViT-B/32", device=_clip_device)
        _clip_model.eval()
        gpu_name = torch.cuda.get_device_name(0) if _clip_device == "cuda" else "CPU"
        print(f"[Vision] CLIP chargé sur {_clip_device} ({gpu_name})")
    except ImportError:
        raise ImportError(
            "CLIP non installé. Lance :\n"
            "  pip install git+https://github.com/openai/CLIP.git pillow"
        )


def _encode_image(pil_image: Image.Image) -> np.ndarray:
    """Encode une PIL Image → vecteur CLIP normalisé (512,)."""
    import torch
    img_tensor = _clip_preprocess(pil_image).unsqueeze(0).to(_clip_device)
    with torch.no_grad():
        embedding = _clip_model.encode_image(img_tensor)
        embedding = embedding / embedding.norm(dim=-1, keepdim=True)
    return embedding.cpu().numpy().flatten()


def _get_pool_text_embeddings() -> np.ndarray:
    """Encode les prompts piscine une seule fois (lazy)."""
    global _pool_text_embeddings
    if _pool_text_embeddings is not None:
        return _pool_text_embeddings
    import torch
    import clip
    tokens = clip.tokenize(POOL_PROMPTS).to(_clip_device)
    with torch.no_grad():
        embs = _clip_model.encode_text(tokens)
        embs = embs / embs.norm(dim=-1, keepdim=True)
    _pool_text_embeddings = embs.cpu().numpy()
    return _pool_text_embeddings


# Détection piscine sur orthophoto (vue aérienne) — classification CLIP contrastive.
# Utilisé par scrapers/geolocate.py pour confirmer un amas turquoise repéré sur l'ortho.
AERIAL_POOL_PROMPTS = [
    "an aerial satellite view of a backyard swimming pool",
    "a rectangular swimming pool seen from directly above",
]
AERIAL_NEG_PROMPTS = [
    "an aerial satellite view of a rooftop",
    "an aerial satellite view of a green garden with grass",
    "an aerial satellite view of trees and vegetation",
    "an aerial satellite view of a road, driveway or parking lot",
    "an aerial satellite view of a blue tarpaulin, trampoline or car",
]
_aerial_pool_text_emb = None  # np.ndarray (pos+neg, 512)


def clip_pool_confidence(pil_image: Image.Image) -> float:
    """
    Probabilité (0–1) qu'un crop d'orthophoto représente une piscine, par
    classification zero-shot CLIP contrastive (softmax sur prompts piscine vs
    prompts négatifs : toit, jardin, route, bâche…).

    Retourne -1.0 si CLIP est indisponible (le détecteur appelant retombe alors
    sur la seule heuristique couleur).
    """
    global _aerial_pool_text_emb
    try:
        _init_clip()
    except ImportError:
        return -1.0

    if _aerial_pool_text_emb is None:
        import torch
        import clip
        prompts = AERIAL_POOL_PROMPTS + AERIAL_NEG_PROMPTS
        tokens = clip.tokenize(prompts).to(_clip_device)
        with torch.no_grad():
            embs = _clip_model.encode_text(tokens)
            embs = embs / embs.norm(dim=-1, keepdim=True)
        _aerial_pool_text_emb = embs.cpu().numpy()

    try:
        emb = _encode_image(pil_image.convert("RGB"))
    except Exception:
        return -1.0

    sims = _aerial_pool_text_emb @ emb            # cosinus (vecteurs normalisés)
    logits = sims * 100.0                          # logit_scale CLIP ≈ 100
    logits -= logits.max()
    exp = np.exp(logits)
    probs = exp / exp.sum()
    return float(probs[:len(AERIAL_POOL_PROMPTS)].sum())


def detect_piscine_in_photos(photos: list) -> bool:
    """Retourne True si au moins une photo ressemble à une piscine (CLIP text-image)."""
    if not photos or _clip_model is None:
        return False
    pool_embs = _get_pool_text_embeddings()
    for img in photos:
        try:
            img_emb = _encode_image(img)
            if max(cosine_similarity(img_emb, pe) for pe in pool_embs) >= POOL_THRESHOLD:
                return True
        except Exception:
            pass
    return False


# ──────────────────────────────────────────────
# DÉTECTEUR D'ÉLÉMENTS INDÉSIRABLES (CLIP zero-shot contrastif)
# ──────────────────────────────────────────────

def load_elements() -> list[dict]:
    """Charge config/elements.yaml (liste d'éléments actifs). Cache session.

    Chaque élément : {nom, positifs[], negatifs[], seuil, mode, actif}.
    Retourne [] si le fichier est absent/vide/illisible (détecteur désactivé).
    """
    global _elements_cfg
    if _elements_cfg is not None:
        return _elements_cfg
    _elements_cfg = []
    if not ELEMENTS_YAML.exists():
        return _elements_cfg
    try:
        data = yaml.safe_load(ELEMENTS_YAML.read_text(encoding="utf-8")) or {}
        for el in data.get("elements", []):
            if not el.get("actif", True):
                continue
            if not el.get("positifs") or not el.get("negatifs"):
                print(f"[Vision]   ⚠️  Élément '{el.get('nom')}' ignoré : positifs/negatifs manquants")
                continue
            _elements_cfg.append({
                "nom":      el.get("nom", "?"),
                "positifs": list(el["positifs"]),
                "negatifs": list(el["negatifs"]),
                "seuil":    float(el.get("seuil", 0.6)),
                "mode":     el.get("mode", "exclusion"),
            })
    except Exception as e:
        print(f"[Vision]   ⚠️  elements.yaml illisible ({e}) — détecteur désactivé")
        _elements_cfg = []
    return _elements_cfg


def _get_element_text_emb(el: dict) -> np.ndarray:
    """Encode (positifs + negatifs) d'un élément → (npos+nneg, 512), caché par nom."""
    nom = el["nom"]
    if nom in _element_text_emb:
        return _element_text_emb[nom]
    import torch
    import clip
    prompts = el["positifs"] + el["negatifs"]
    tokens = clip.tokenize(prompts).to(_clip_device)
    with torch.no_grad():
        embs = _clip_model.encode_text(tokens)
        embs = embs / embs.norm(dim=-1, keepdim=True)
    _element_text_emb[nom] = embs.cpu().numpy()
    return _element_text_emb[nom]


def _element_confidence(img_emb: np.ndarray, el: dict) -> float:
    """Probabilité (0–1, softmax contrastif) que la photo contienne l'élément."""
    T = _get_element_text_emb(el)
    npos = len(el["positifs"])
    logits = (T @ img_emb) * 100.0          # logit_scale CLIP ≈ 100
    logits -= logits.max()
    exp = np.exp(logits)
    probs = exp / exp.sum()
    return float(probs[:npos].sum())


def detect_elements(photos: list) -> list[dict]:
    """Détecte les éléments indésirables (elements.yaml) dans les photos d'un bien.

    Pour chaque élément actif, score = MAX de la confiance sur toutes les photos
    (une seule photo suffit à le présenter). Retourne la liste des éléments dont
    le score >= seuil : [{nom, score, photo_idx, mode}], trié par score décroissant.
    """
    elements = load_elements()
    if not elements or not photos:
        return []
    img_embs = []
    for p in photos:
        try:
            img_embs.append(_encode_image(p))
        except Exception:
            pass
    if not img_embs:
        return []
    detected = []
    for el in elements:
        best_s, best_i = -1.0, -1
        for i, emb in enumerate(img_embs):
            s = _element_confidence(emb, el)
            if s > best_s:
                best_s, best_i = s, i
        if best_s >= el["seuil"]:
            detected.append({
                "nom": el["nom"], "score": round(best_s, 2),
                "photo_idx": best_i, "mode": el["mode"],
            })
    detected.sort(key=lambda d: d["score"], reverse=True)
    return detected


# ──────────────────────────────────────────────
# TÉLÉCHARGEMENT PHOTOS D'ANNONCE
# ──────────────────────────────────────────────

async def fetch_photo(url: str, session: httpx.AsyncClient) -> Optional[Image.Image]:
    """Télécharge une photo et retourne une PIL Image."""
    try:
        r = await session.get(url, timeout=10, follow_redirects=True)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        return None


async def fetch_bien_photos(bien: dict, session: httpx.AsyncClient, limit: int = MAX_PHOTOS_PISCINE) -> list[Image.Image]:
    """Récupère jusqu'à `limit` photos d'un bien."""
    urls = (bien.get("photos") or [])[:limit]
    if not urls:
        return []
    results = await asyncio.gather(*[fetch_photo(u, session) for u in urls])
    return [r for r in results if r is not None]


# ──────────────────────────────────────────────
# SCORING CLIP
# ──────────────────────────────────────────────

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Similarité cosinus entre deux vecteurs normalisés."""
    return float(np.dot(a, b))


# ──────────────────────────────────────────────
# ÉVALUATION D'UN BIEN
# ──────────────────────────────────────────────

async def evaluate_bien(
    bien: dict,
    session: httpx.AsyncClient,
) -> dict:
    """Évalue les photos d'un bien (éléments indésirables + piscine), enrichit le dict."""
    photos_all = await fetch_bien_photos(bien, session, MAX_PHOTOS_PISCINE)

    # Éléments indésirables (elements.yaml) détectés dans les photos
    elements = detect_elements(photos_all)
    excluants = [e for e in elements if e["mode"] == "exclusion"]

    bien["elements_detectes"] = elements
    bien["banni"] = bool(excluants)
    bien["nb_photos_analysees"] = len(photos_all)
    bien["piscine_visuelle"] = detect_piscine_in_photos(photos_all)

    if not photos_all:
        bien["resume_visuel"] = "Aucune photo disponible"
    else:
        pool_tag = " | piscine détectée" if bien["piscine_visuelle"] else ""
        if excluants:
            elem_tag = " | ⛔ " + ", ".join(f"{e['nom']} ({e['score']})" for e in excluants)
        elif elements:
            elem_tag = " | ⚠️ " + ", ".join(f"{e['nom']} ({e['score']})" for e in elements)
        else:
            elem_tag = ""
        bien["resume_visuel"] = (
            f"{len(photos_all)} photo(s) analysée(s) en local{pool_tag}{elem_tag}"
        )

    return bien


# ──────────────────────────────────────────────
# PIPELINE PRINCIPAL
# ──────────────────────────────────────────────

async def run(biens: list[dict], concurrency: int = 8) -> list[dict]:
    """
    Filtre les biens par présence d'éléments indésirables (config/elements.yaml).
    Biens sans photos : conservés (non évalués). Biens contenant un élément en
    mode exclusion : rejetés. Éléments en mode alerte : annotés, conservés.
    """
    try:
        _init_clip()
    except ImportError as e:
        print(f"[Vision] ⚠️  {e} — filtre visuel désactivé")
        return biens

    elements = load_elements()
    if not elements:
        print("[Vision] Aucun élément dans elements.yaml — filtre visuel désactivé")
        return biens

    print(f"[Vision] Analyse de {len(biens)} biens ({len(elements)} élément(s) surveillé(s))")

    semaphore = asyncio.Semaphore(concurrency)

    async def eval_with_sem(bien, session):
        async with semaphore:
            return await evaluate_bien(bien, session)

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; immo-agent/1.0)"},
        timeout=15,
    ) as session:
        results = await asyncio.gather(*[eval_with_sem(b, session) for b in biens])

    # Filtrage + alertes
    kept, excluded_elem = [], []
    for b in results:
        # Hard-exclude : un élément indésirable (mode exclusion) est présent
        if b.get("banni"):
            excluded_elem.append(b)
            continue
        # Alerte : élément indésirable en mode alerte (non excluant)
        for e in b.get("elements_detectes", []):
            if e["mode"] != "exclusion":
                b.setdefault("alerte", []).append(f"⚠️ {e['nom']} ({e['score']})")
        kept.append(b)

    elem_msg = f" | Écartés (élément indésirable) : {len(excluded_elem)}" if excluded_elem else ""
    print(f"[Vision] ✓ Conservés : {len(kept)}{elem_msg}")
    return kept


async def rescore_elements(biens: list[dict], concurrency: int = 8) -> int:
    """Garde-fou rétroactif : (re)détecte les éléments indésirables sur les entrées
    LEGACY du suivi (jamais évaluées contre elements.yaml, clé `elements_detectes`
    absente). Mute `elements_detectes` et `banni` en place.

    Sert au suivi cumulatif : un élément ajouté à elements.yaml APRÈS qu'un bien y
    est entré n'a jamais été confronté à ce bien (`filter_new` empêche son
    re-scoring). Coût borné : seules les entrées jamais évaluées sont re-téléchargées ;
    une fois `elements_detectes` posé, elles ne le sont plus.
    Retourne le nombre de biens nouvellement marqués pour exclusion.
    """
    if not load_elements():
        return 0
    targets = [b for b in biens if "elements_detectes" not in b and b.get("photos")]
    if not targets:
        return 0
    try:
        _init_clip()
    except ImportError:
        return 0

    sem = asyncio.Semaphore(concurrency)

    async def one(b: dict, session: httpx.AsyncClient):
        async with sem:
            photos = await fetch_bien_photos(b, session, MAX_PHOTOS_PISCINE)
            if not photos:
                return  # pas de photo récupérable → on laisse elements_detectes absent
            elements = detect_elements(photos)
            b["elements_detectes"] = elements
            b["banni"] = any(e["mode"] == "exclusion" for e in elements)

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; immo-agent/1.0)"},
        timeout=15,
    ) as session:
        await asyncio.gather(*[one(b, session) for b in targets])

    return sum(1 for b in targets if b.get("banni"))


async def _calibrer(nom: str, biens: list[dict]):
    """Affiche le score d'un élément sur chaque bien (toutes photos confondues),
    trié décroissant, pour choisir le seuil. Le bien n'est PAS filtré ici."""
    # On lit l'élément même s'il est actif:false (calibration avant activation).
    data = yaml.safe_load(ELEMENTS_YAML.read_text(encoding="utf-8")) or {}
    el = next((e for e in data.get("elements", []) if e.get("nom") == nom), None)
    if not el:
        print(f"Élément '{nom}' introuvable dans {ELEMENTS_YAML.name}")
        return
    el = {"nom": nom, "positifs": el["positifs"], "negatifs": el["negatifs"],
          "seuil": float(el.get("seuil", 0.6)), "mode": el.get("mode", "exclusion")}
    _init_clip()
    rows = []
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}, timeout=15) as s:
        sem = asyncio.Semaphore(8)
        async def one(b):
            async with sem:
                photos = await fetch_bien_photos(b, s, MAX_PHOTOS_PISCINE)
                if not photos:
                    return
                embs = []
                for p in photos:
                    try: embs.append(_encode_image(p))
                    except Exception: pass
                if not embs:
                    return
                best_i = max(range(len(embs)), key=lambda i: _element_confidence(embs[i], el))
                rows.append((_element_confidence(embs[best_i], el), best_i, len(photos), b))
        await asyncio.gather(*[one(b) for b in biens])
    rows.sort(key=lambda r: -r[0])
    print(f"\nCalibration '{nom}' (seuil actuel {el['seuil']}) — score | photo#/n | bien")
    for sc, idx, n, b in rows:
        flag = "  ◄ AU-DESSUS DU SEUIL" if sc >= el["seuil"] else ""
        print(f"  {sc:5.2f} | #{idx}/{n} | {(b.get('ville') or '?')[:16]:16} {(b.get('url') or '')[:55]}{flag}")


if __name__ == "__main__":
    import json, sys

    raw_files = sorted(Path("data/raw").glob("biens_raw_*.json"))
    if not raw_files:
        print("Aucun fichier raw. Lance d'abord hunter.py")
        sys.exit(1)
    biens = json.loads(raw_files[-1].read_text(encoding="utf-8"))

    if len(sys.argv) >= 3 and sys.argv[1] == "--calibrer":
        print(f"Calibration sur {len(biens)} biens depuis {raw_files[-1].name}")
        asyncio.run(_calibrer(sys.argv[2], biens))
        sys.exit(0)

    print(f"Test CLIP sur {len(biens)} biens depuis {raw_files[-1]}")
    result = asyncio.run(run(biens))
    print(f"\nRésultat : {len(result)} biens conservés")
    for b in result[:10]:
        els = b.get("elements_detectes") or []
        tag = " ⚠️ " + ", ".join(f"{e['nom']}({e['score']})" for e in els) if els else ""
        print(f"  {b.get('titre', '')[:60]}{tag}")

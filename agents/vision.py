"""
agents/vision.py — Agent Vision (CLIP local)
Évalue la correspondance stylistique des photos d'annonces
avec les références visuelles définies dans config/style_references/.

Fonctionnement :
1. Au démarrage : charge CLIP en local (ViT-B/32, ~600 MB, téléchargé une fois)
2. Encode toutes les photos de référence → vecteurs (fait une seule fois par session)
3. Pour chaque bien : télécharge ses photos, encode, calcule similarité cosinus vs références
4. Score = moyenne des top-3 similarités, normalisée 0–100
5. Filtrage selon seuils définis dans criteria.md

Aucune donnée ne quitte ta machine. Gratuit, illimité en photos de référence.

Seuils configurables dans criteria.md (section ## Style visuel) :
  style_seuil_exclusion: 30   # score style < X → rejeté
  style_seuil_warning:   55   # score style < X → alerte
  style_seuil_ban:       70   # score ban > X   → rejeté (ressemble trop à un bien banni)

Dossiers d'images :
  config/style_references/   images de ce que tu VEUX   (maisons de caractère, longères…)
  config/style_ban/          images de ce que tu NE VEUX PAS (pavillons, béton, toit plat…)
"""

import asyncio
import re
from pathlib import Path
from typing import Optional
import numpy as np
import httpx
from PIL import Image
import io

# Chargement lazy de CLIP pour ne pas bloquer l'import si non installé
_clip_model = None
_clip_preprocess = None
_clip_device = None
_ref_embeddings = None      # np.ndarray (N, 512) — calculé une fois
_ref_labels = None          # list[str] — noms de fichiers
_ban_embeddings = None      # np.ndarray (B, 512) — images à bannir
_ban_labels = None          # list[str] — noms de fichiers ban
_pool_text_embeddings = None  # np.ndarray (P, 512) — requêtes texte piscine


STYLE_DIR = Path(__file__).parent.parent / "config" / "style_references"
BAN_DIR   = Path(__file__).parent.parent / "config" / "style_ban"
CRITERIA_MD = Path(__file__).parent.parent / "config" / "criteria.md"

DEFAULT_SEUIL_EXCLUSION = 30
DEFAULT_SEUIL_WARNING = 55
DEFAULT_SEUIL_BAN = 70
MAX_PHOTOS_PAR_BIEN = 5     # photos utilisées pour le scoring de style
MAX_PHOTOS_PISCINE = 15    # photos téléchargées pour la détection piscine (souvent en fin de galerie)
TOP_K_SIMILARITIES = 3      # on moyenne les top-K scores (robustesse)

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
        import clip
        import torch
        _clip_device = "cuda" if torch.cuda.is_available() else "cpu"
        _clip_model, _clip_preprocess = clip.load("ViT-B/32", device=_clip_device)
        _clip_model.eval()
        print(f"[Vision] CLIP chargé sur {_clip_device}")
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
# CHARGEMENT DES RÉFÉRENCES
# ──────────────────────────────────────────────

def load_reference_embeddings() -> tuple[np.ndarray, list[str]]:
    """
    Charge et encode toutes les images de style_references/.
    Résultat mis en cache global — calculé une seule fois par session.
    """
    global _ref_embeddings, _ref_labels
    if _ref_embeddings is not None:
        return _ref_embeddings, _ref_labels

    _init_clip()

    supported = {".jpg", ".jpeg", ".png", ".webp"}
    paths = [p for p in sorted(STYLE_DIR.iterdir()) if p.suffix.lower() in supported]

    if not paths:
        print("[Vision] ⚠️  Aucune image de référence trouvée dans style_references/")
        _ref_embeddings = np.empty((0, 512))
        _ref_labels = []
        return _ref_embeddings, _ref_labels

    print(f"[Vision] Encodage de {len(paths)} image(s) de référence...")
    embeddings, labels = [], []
    for path in paths:
        try:
            img = Image.open(path).convert("RGB")
            emb = _encode_image(img)
            embeddings.append(emb)
            labels.append(path.name)
        except Exception as e:
            print(f"[Vision]   ⚠️  Ignoré {path.name} : {e}")

    _ref_embeddings = np.stack(embeddings) if embeddings else np.empty((0, 512))
    _ref_labels = labels
    print(f"[Vision] {len(labels)} référence(s) encodée(s) ✓")
    return _ref_embeddings, _ref_labels


def load_ban_embeddings() -> tuple[np.ndarray, list[str]]:
    """
    Charge et encode toutes les images de style_ban/.
    Résultat mis en cache global — calculé une seule fois par session.
    Si le dossier n'existe pas ou est vide, retourne un tableau vide (ban désactivé).
    """
    global _ban_embeddings, _ban_labels
    if _ban_embeddings is not None:
        return _ban_embeddings, _ban_labels

    _init_clip()

    if not BAN_DIR.exists():
        _ban_embeddings = np.empty((0, 512))
        _ban_labels = []
        return _ban_embeddings, _ban_labels

    supported = {".jpg", ".jpeg", ".png", ".webp"}
    paths = [p for p in sorted(BAN_DIR.iterdir()) if p.suffix.lower() in supported]

    if not paths:
        print("[Vision] ℹ️  Dossier style_ban/ vide — filtre ban désactivé")
        _ban_embeddings = np.empty((0, 512))
        _ban_labels = []
        return _ban_embeddings, _ban_labels

    print(f"[Vision] Encodage de {len(paths)} image(s) ban...")
    embeddings, labels = [], []
    for path in paths:
        try:
            img = Image.open(path).convert("RGB")
            emb = _encode_image(img)
            embeddings.append(emb)
            labels.append(path.name)
        except Exception as e:
            print(f"[Vision]   ⚠️  Ignoré ban/{path.name} : {e}")

    _ban_embeddings = np.stack(embeddings) if embeddings else np.empty((0, 512))
    _ban_labels = labels
    print(f"[Vision] {len(labels)} image(s) ban encodée(s) ✓")
    return _ban_embeddings, _ban_labels


# ──────────────────────────────────────────────
# SEUILS DEPUIS criteria.md
# ──────────────────────────────────────────────

def load_style_seuils() -> tuple[int, int, int]:
    """Parse style_seuil_exclusion, style_seuil_warning et style_seuil_ban depuis criteria.md."""
    try:
        content = CRITERIA_MD.read_text(encoding="utf-8")
        excl = _parse_val(content, "style_seuil_exclusion", DEFAULT_SEUIL_EXCLUSION)
        warn = _parse_val(content, "style_seuil_warning",   DEFAULT_SEUIL_WARNING)
        ban  = _parse_val(content, "style_seuil_ban",       DEFAULT_SEUIL_BAN)
        return excl, warn, ban
    except Exception:
        return DEFAULT_SEUIL_EXCLUSION, DEFAULT_SEUIL_WARNING, DEFAULT_SEUIL_BAN


def _parse_val(text: str, key: str, default: int) -> int:
    m = re.search(rf"{key}\s*:\s*(\d+)", text)
    return int(m.group(1)) if m else default


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


def score_photos_vs_refs(
    bien_photos: list[Image.Image],
    ref_embeddings: np.ndarray,
) -> tuple[float, list[float]]:
    """
    Score de similarité stylistique entre les photos d'un bien et les références.

    Stratégie :
    - Pour chaque photo du bien, calcule sa similarité max avec les références
    - Le score du bien = moyenne des similarités des meilleures photos (top-K)
    - Normalisé : similarité cosinus CLIP typiquement entre 0.2–0.95

    Retourne (score_0_100, liste_scores_par_photo).
    """
    if len(ref_embeddings) == 0 or not bien_photos:
        return 50.0, []

    photo_scores = []
    for img in bien_photos:
        try:
            emb = _encode_image(img)
            # Similarité avec chaque référence → prend le max (meilleur match)
            sims = [cosine_similarity(emb, ref) for ref in ref_embeddings]
            photo_scores.append(max(sims))
        except Exception:
            pass

    if not photo_scores:
        return 50.0, []

    # Moyenne des top-K meilleurs scores (ignore les photos hors-sujet, ex: plan)
    top_k = sorted(photo_scores, reverse=True)[:TOP_K_SIMILARITIES]
    raw_score = float(np.mean(top_k))

    # Normalisation : CLIP cosine sim est typiquement entre 0.15 (rien à voir)
    # et 0.90 (quasi-identique). On mappe [0.20, 0.75] → [0, 100]
    normalized = (raw_score - 0.20) / (0.75 - 0.20) * 100
    score = max(0.0, min(100.0, normalized))

    return round(score, 1), [round(s * 100, 1) for s in photo_scores]


def score_photos_vs_ban(
    bien_photos: list[Image.Image],
    ban_embeddings: np.ndarray,
) -> float:
    """
    Score de similarité entre les photos d'un bien et les images ban.
    Retourne le score 0–100 (plus c'est élevé, plus ça ressemble à ce qu'on NE veut PAS).
    Utilise le max des top-K pour être conservateur (une seule photo très similaire suffit).
    """
    if len(ban_embeddings) == 0 or not bien_photos:
        return 0.0

    photo_scores = []
    for img in bien_photos:
        try:
            emb = _encode_image(img)
            sims = [cosine_similarity(emb, ref) for ref in ban_embeddings]
            photo_scores.append(max(sims))
        except Exception:
            pass

    if not photo_scores:
        return 0.0

    # On prend le max global : une seule photo "ban" suffit à exclure
    raw_score = max(photo_scores)
    normalized = (raw_score - 0.20) / (0.75 - 0.20) * 100
    return round(max(0.0, min(100.0, normalized)), 1)


def verdict(score: float, seuil_excl: int, seuil_warn: int) -> str:
    if score >= seuil_warn:
        return "match"
    elif score >= seuil_excl:
        return "partiel"
    else:
        return "exclu"


# ──────────────────────────────────────────────
# ÉVALUATION D'UN BIEN
# ──────────────────────────────────────────────

async def evaluate_bien(
    bien: dict,
    ref_embeddings: np.ndarray,
    ref_labels: list[str],
    ban_embeddings: np.ndarray,
    seuil_excl: int,
    seuil_warn: int,
    seuil_ban: int,
    session: httpx.AsyncClient,
) -> dict:
    """Évalue visuellement un bien, enrichit le dict et le retourne."""
    photos_all = await fetch_bien_photos(bien, session, MAX_PHOTOS_PISCINE)
    photos_style = photos_all[:MAX_PHOTOS_PAR_BIEN]

    # Score positif (similarité aux références souhaitées)
    score, per_photo = score_photos_vs_refs(photos_style, ref_embeddings)

    # Score ban (similarité aux références indésirables)
    score_ban = score_photos_vs_ban(photos_style, ban_embeddings)

    bien["score_visuel"] = score
    bien["score_ban"] = score_ban
    bien["banni"] = (len(ban_embeddings) > 0) and (score_ban >= seuil_ban)
    bien["verdict_visuel"] = verdict(score, seuil_excl, seuil_warn)
    bien["nb_photos_analysees"] = len(photos_all)
    bien["scores_par_photo"] = per_photo
    bien["piscine_visuelle"] = detect_piscine_in_photos(photos_all)

    if not photos_all:
        bien["resume_visuel"] = "Aucune photo disponible — score non calculé"
        bien["score_visuel"] = None
    else:
        pool_tag = " | piscine détectée" if bien["piscine_visuelle"] else ""
        ban_tag  = f" | ⛔ banni (score ban {score_ban:.0f}/100)" if bien["banni"] else ""
        bien["resume_visuel"] = (
            f"{len(photos_all)} photo(s) analysée(s) en local — "
            f"similarité style : {score:.0f}/100{pool_tag}{ban_tag}"
        )

    return bien


# ──────────────────────────────────────────────
# PIPELINE PRINCIPAL
# ──────────────────────────────────────────────

async def run(biens: list[dict], concurrency: int = 8) -> list[dict]:
    """
    Filtre et score visuellement une liste de biens via CLIP local.
    Retourne la liste filtrée, triée par score_visuel décroissant.
    Biens sans photos : conservés avec score_visuel=None, non exclus.
    Biens dont le score_ban >= seuil_ban : rejetés (hard-exclude).
    """
    seuil_excl, seuil_warn, seuil_ban = load_style_seuils()

    try:
        ref_embeddings, ref_labels = load_reference_embeddings()
    except ImportError as e:
        print(f"[Vision] ⚠️  {e} — filtre visuel désactivé")
        return biens

    if len(ref_embeddings) == 0:
        print("[Vision] Aucune référence — filtre visuel désactivé")
        return biens

    # Chargement des images ban (optionnel — silence si dossier absent)
    try:
        ban_embeddings, ban_labels = load_ban_embeddings()
    except Exception:
        ban_embeddings = np.empty((0, 512))
        ban_labels = []

    ban_info = f" | {len(ban_labels)} ban" if ban_labels else ""
    print(f"[Vision] Scoring de {len(biens)} biens "
          f"({len(ref_labels)} références{ban_info} | "
          f"seuil excl={seuil_excl} warn={seuil_warn} ban={seuil_ban})")

    semaphore = asyncio.Semaphore(concurrency)

    async def eval_with_sem(bien, session):
        async with semaphore:
            return await evaluate_bien(
                bien, ref_embeddings, ref_labels,
                ban_embeddings,
                seuil_excl, seuil_warn, seuil_ban,
                session,
            )

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; immo-agent/1.0)"},
        timeout=15,
    ) as session:
        results = await asyncio.gather(*[eval_with_sem(b, session) for b in biens])

    # Filtrage + alertes
    kept, excluded_style, excluded_ban = [], [], []
    for b in results:
        # Hard-exclude : ressemble trop aux images ban
        if b.get("banni"):
            excluded_ban.append(b)
            continue
        score = b.get("score_visuel")
        # Hard-exclude : score style insuffisant
        if score is not None and score < seuil_excl:
            excluded_style.append(b)
            continue
        # Alerte : score partiel
        if score is not None and score < seuil_warn:
            b.setdefault("alerte", []).append(
                f"🎨 Style partiel (score visuel {score:.0f}/100)"
            )
        kept.append(b)

    # Tri : biens avec score d'abord (desc), puis sans photo à la fin
    kept.sort(key=lambda b: b.get("score_visuel") or -1, reverse=True)

    ban_msg = f" | Bannis (style indésirable) : {len(excluded_ban)}" if excluded_ban else ""
    print(f"[Vision] ✓ Conservés : {len(kept)} | Exclus (style) : {len(excluded_style)}{ban_msg}")
    return kept


if __name__ == "__main__":
    import json, sys

    raw_files = sorted(Path("data/raw").glob("*.json"))
    if not raw_files:
        print("Aucun fichier raw. Lance d'abord hunter.py")
        sys.exit(1)

    biens = json.loads(raw_files[-1].read_text(encoding="utf-8"))
    print(f"Test CLIP sur {len(biens)} biens depuis {raw_files[-1]}")
    result = asyncio.run(run(biens))
    print(f"\nRésultat : {len(result)} biens conservés")
    for b in result[:10]:
        score = b.get("score_visuel")
        score_str = f"{score:.0f}/100" if score is not None else "N/A"
        print(f"  {score_str:8s} [{b.get('verdict_visuel', '?'):8s}] {b.get('titre', '')[:55]}")

"""core/filters.py — Filtres a posteriori partagés.

Centralise la séquence de filtrage auparavant dupliquée dans hunter.run,
orchestrator (--only-analyse) et scheduler.refilter_suivi :
  filtre structurel (prix/surface/pièces/terrain/DPE) → extraction terrain depuis
  le texte → mots-clés obligatoires/interdits → photos_min.

Les fonctions sont PURES (pas d'I/O réseau) et travaillent sur des `list[dict]`,
ce qui les rend testables unitairement.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

from core.dept_data import filter_by_dept

# ──────────────────────────────────────────────
# NORMALISATION TEXTE
# ──────────────────────────────────────────────

def normalize_text(s: str) -> str:
    """Minuscules, sans accents — pour comparer du texte d'annonce aux mots-clés."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


# Marqueurs (texte normalisé) trahissant un mot mentionné sans qu'il soit RÉEL :
#  - absence, JUSTE avant le mot (« sans piscine », « pas de piscine ») ;
#  - souhait / potentiel, dans une fenêtre plus large avant (« on rêverait d'une
#    piscine », « emplacement pour piscine », « possibilité de créer une piscine »).
_ABSENCE_BEFORE = ("sans ", "pas de ", "pas d", "aucune ", "aucun ", "ni ", "ni de ")
_WISH_BEFORE = (
    "rever", "imagin", "possib", "emplacement", "creer", "creation", "constru",
    "pourrait", "permet", "envisage", "projet", "prevoir",
    "place pour", "espace pour", "ideal pour", "parfait pour", "pour une", "pour y",
    "pour installer", "pour amenager", "pour accueillir", "potentiel", "amenageable",
)
_WISH_AFTER = (
    "a creer", "a amenager", "a prevoir", "a construire", "possible", "envisageable",
    "realisable", "en projet", "potentiel",
)


def present_affirmative(texte: str, mot: str) -> bool:
    """True si `mot` apparaît au moins une fois en contexte AFFIRMATIF dans `texte`.

    Écarte les mentions d'absence (« sans piscine ») ou de simple potentiel
    (« on rêverait d'une piscine », « emplacement pour piscine »). `texte` et `mot`
    sont supposés déjà normalisés (minuscules, sans accents)."""
    start = 0
    while True:
        pos = texte.find(mot, start)
        if pos == -1:
            return False
        before = texte[max(0, pos - 45):pos]
        after = texte[pos + len(mot):pos + len(mot) + 25]
        near_absence = any(before.endswith(a) or before.endswith(a + "la ")
                           or before.endswith(a + "une ") or before.endswith(a + "grande ")
                           for a in _ABSENCE_BEFORE)
        if not near_absence \
           and not any(w in before for w in _WISH_BEFORE) \
           and not any(w in after for w in _WISH_AFTER):
            return True                       # occurrence affirmative trouvée
        start = pos + len(mot)


def filter_mots_cles(biens: list[dict], criteres) -> list[dict]:
    """Filtre dur sur le TEXTE de l'annonce (titre + description COMPLÈTE).

    Appliqué APRÈS l'enrichissement page détail (la description complète n'existe
    qu'à ce moment), contrairement aux critères structurés qui filtrent au requêtage.
      - mots_obligatoires : tous doivent figurer en contexte AFFIRMATIF (logique ET) —
        une mention d'absence/souhait (« on rêverait d'une piscine ») ne compte pas ;
      - mots_interdits    : un seul présent (sous-chaîne) ⇒ exclu.
    Insensible à la casse/accents."""
    mots_oblig = [normalize_text(m) for m in getattr(criteres, "mots_obligatoires", []) or []]
    mots_int = [normalize_text(m) for m in getattr(criteres, "mots_interdits", []) or []]
    if not (mots_oblig or mots_int):
        return biens

    kept = []
    for b in biens:
        texte = normalize_text(f"{b.get('titre', '')} {b.get('description', '')}")
        if any(m in texte for m in mots_int):
            continue
        if mots_oblig and not all(present_affirmative(texte, m) for m in mots_oblig):
            continue
        kept.append(b)
    return kept


# ──────────────────────────────────────────────
# EXTRACTION TERRAIN DEPUIS LE TEXTE
# ──────────────────────────────────────────────

# Conservateur : ne retient qu'un nombre clairement rattaché à « terrain »/« parcelle »
# et suivi de m²/m2, et prend la valeur MAX trouvée (évite d'exclure à tort un bien
# dont on aurait capté un petit nombre annexe).
_TERRAIN_NUM = r"(\d[\d  .,]*\d|\d)"
_TERRAIN_RES = (
    # « terrain de 412 m² », « parcelle d'environ 4 172 m² »
    re.compile(r"(?:terrain|parcelle)[^.\n]{0,40}?" + _TERRAIN_NUM + r"\s*m(?:²|2)\b", re.IGNORECASE),
    # « 4 292 m² de terrain », « 13210 m² de parcelle »
    re.compile(_TERRAIN_NUM + r"\s*m(?:²|2)\s+(?:de\s+|d['’]\s*|environ\s+)*(?:terrain|parcelle)", re.IGNORECASE),
)


def extract_terrain_from_text(texte: str) -> Optional[float]:
    """Surface de terrain (m²) déduite du texte, None si rien de fiable.

    Best-effort, conservateur : un nombre doit être collé à « terrain »/« parcelle »
    ET suivi de m². Retourne le MAX des valeurs plausibles (50 ≤ v ≤ 2 000 000)."""
    if not texte:
        return None
    best = None
    for rx in _TERRAIN_RES:
        for m in rx.finditer(texte):
            digits = re.sub(r"[  .,]", "", m.group(1))
            if not digits.isdigit():
                continue
            val = float(digits)
            if 50 <= val <= 2_000_000 and (best is None or val > best):
                best = val
    return best


# ──────────────────────────────────────────────
# FILTRE STRUCTUREL
# ──────────────────────────────────────────────

def filter_biens(biens: list[dict], criteres) -> list[dict]:
    """Applique les filtres d'exclusion durs (champs STRUCTURÉS)."""
    filtered = []
    for b in biens:
        # DPE exclu
        dpe = b.get("dpe", "")
        if dpe and dpe.upper() in [d.upper() for d in criteres.dpe_exclus]:
            continue

        # Prix min / max
        prix = b.get("prix")
        if prix and prix > criteres.prix_max:
            continue
        if prix and getattr(criteres, "prix_min", 0) and prix < criteres.prix_min:
            continue

        # Surface habitable min / max
        surface = b.get("surface")
        if surface and surface < criteres.surface_min:
            continue
        if surface and getattr(criteres, "surface_max", 0) and surface > criteres.surface_max:
            continue

        # Terrain min (surface_terrain) — un bien sans terrain renseigné n'est PAS exclu.
        terrain = b.get("surface_terrain")
        if terrain and getattr(criteres, "terrain_min", 0) and terrain < criteres.terrain_min:
            continue

        # Pièces min / max
        pieces = b.get("pieces")
        if pieces and getattr(criteres, "pieces_min", 0) and pieces < criteres.pieces_min:
            continue
        if pieces and getattr(criteres, "pieces_max", 0) and pieces > criteres.pieces_max:
            continue

        # NB : photos_min n'est PAS appliqué ici — la vue liste ne capte que 0-1 photo.
        # Il l'est après l'enrichissement galerie (page détail).

        filtered.append(b)

    return filtered


def refilter_dpe(biens: list[dict], criteres) -> list[dict]:
    """Ré-applique `dpe_exclus` : le DPE n'est souvent capté qu'APRÈS la page
    détail (gallery l'extrait) — les passoires F/G captées tardivement sont
    écartées ici. No-op si dpe_exclus est vide."""
    excl = [str(d).upper() for d in getattr(criteres, "dpe_exclus", []) or []]
    if not excl:
        return biens
    return [b for b in biens
            if not (b.get("dpe") and str(b["dpe"]).upper() in excl)]


def refilter_terrain_from_text(biens: list[dict], criteres) -> list[dict]:
    """Sur les biens sans `surface_terrain`, tente d'extraire le terrain depuis la
    description COMPLÈTE (marque `terrain_estime_texte`), puis ré-applique terrain_min.

    Beaucoup de scrapers ne renseignent pas surface_terrain (il n'est que dans le
    texte). No-op si terrain_min vaut 0."""
    tmin = getattr(criteres, "terrain_min", 0)
    if not tmin:
        return biens
    for b in biens:
        if not b.get("surface_terrain"):
            t = extract_terrain_from_text(b.get("description") or "")
            if t:
                b["surface_terrain"] = t
                b["terrain_estime_texte"] = True   # trace : valeur déduite du texte
    return [b for b in biens
            if not (b.get("surface_terrain") and b["surface_terrain"] < tmin)]


def filter_photos_min(biens: list[dict], criteres) -> list[dict]:
    """Exclut les annonces avec moins de `photos_min` photos. No-op si 0."""
    pmin = getattr(criteres, "photos_min", 0)
    if not pmin:
        return biens
    return [b for b in biens if len(b.get("photos") or []) >= pmin]


def apply_posterior_filters(biens: list[dict], criteres, *, dept_guard: bool = False) -> list[dict]:
    """Séquence complète des filtres a posteriori, sur données DÉJÀ enrichies.

    Ordre : (garde-fou département) → structurel/DPE → terrain depuis texte →
    mots-clés → photos_min. Utilisé par orchestrator (--only-analyse) et
    scheduler.refilter_suivi. Ne fait AUCune requête réseau."""
    if dept_guard:
        biens = filter_by_dept(biens, getattr(criteres, "departements", []) or [])
    biens = filter_biens(biens, criteres)
    biens = refilter_terrain_from_text(biens, criteres)
    biens = filter_mots_cles(biens, criteres)
    biens = filter_photos_min(biens, criteres)
    return biens

"""
qualitative.py — Matching sémantique description qualitative ↔ annonce (NLP).

Compare la `description_qualitative` (criteria.md, texte libre en français) au
texte de chaque annonce (titre + description) via des embeddings de phrases
(sentence-transformers, multilingue). NON destructif : annote chaque bien d'un
  • `match_qualitatif` : similarité 0–100 (None si l'annonce n'a pas de texte) ;
  • `match_extrait`    : la phrase de l'annonce la plus proche (preuve lisible).
L'analyst affiche ces champs (colonnes « Match qual. » / « Extrait qual. ») et
trie les résultats par `match_qualitatif` décroissant.

Dégradation gracieuse : si sentence-transformers / le modèle sont indisponibles
(lib absente, pas de réseau pour le télécharger…), la fonction no-op avec un
avertissement — le pipeline continue sans cette dimension, rien n'est éliminé.

Modèle : paraphrase-multilingual-MiniLM-L12-v2 (~120 Mo, CPU OK, FR inclus).
"""
import re

_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_model = None
_load_failed = False


def _get_model():
    """Charge le modèle une seule fois (singleton paresseux)."""
    global _model, _load_failed
    if _model is not None or _load_failed:
        return _model
    try:
        from sentence_transformers import SentenceTransformer
        print(f"[Qualitatif] Chargement du modèle {_MODEL_NAME.split('/')[-1]}…")
        _model = SentenceTransformer(_MODEL_NAME)
    except Exception as e:
        _load_failed = True
        print(f"[Qualitatif] Modèle indisponible ({type(e).__name__}: {e}) "
              f"— dimension qualitative ignorée.")
        _model = None
    return _model


def _phrases(texte: str) -> list[str]:
    """Découpe grossièrement un texte en phrases exploitables."""
    parts = re.split(r"[.;!?\n•]+", texte)
    return [p.strip() for p in parts if len(p.strip()) >= 12]


def annotate_biens(biens: list[dict], description_qualitative: str) -> list[dict]:
    """Annote `match_qualitatif` (0–100) et `match_extrait` sur chaque bien.

    Idempotent et sûr : retourne `biens` inchangé si la description est vide,
    s'il n'y a aucun bien, ou si le modèle ne peut être chargé."""
    desc = (description_qualitative or "").strip()
    if not desc or not biens:
        return biens

    model = _get_model()
    if model is None:
        return biens

    import numpy as np

    # Vecteur de la requête qualitative (normalisé → produit scalaire = cosinus)
    q = model.encode(desc, convert_to_numpy=True, normalize_embeddings=True)

    textes = [((b.get("titre") or "") + ". " + (b.get("description") or "")).strip()
              for b in biens]

    # Similarité document entier (stable, peu sensible au bruit local)
    doc_vecs = model.encode(textes, convert_to_numpy=True, normalize_embeddings=True,
                            batch_size=64, show_progress_bar=False)
    doc_sims = doc_vecs @ q

    n_ann = 0
    for b, txt, dsim in zip(biens, textes, doc_sims):
        if len(txt) < 12:
            b["match_qualitatif"] = None
            b["match_extrait"] = ""
            continue

        # Meilleure phrase comme preuve, et score = max(doc, meilleure phrase)
        # pour capter une correspondance locale forte sans sur-réagir au bruit.
        best = float(dsim)
        extrait = ""
        phs = _phrases(txt)
        if phs:
            pv = model.encode(phs, convert_to_numpy=True, normalize_embeddings=True,
                              batch_size=64, show_progress_bar=False)
            sims = pv @ q
            j = int(np.argmax(sims))
            extrait = phs[j]
            best = max(best, float(sims[j]))

        # cosinus [-1,1] → [0,100] (les négatifs = aucun rapport → 0)
        b["match_qualitatif"] = round(max(0.0, best) * 100, 1)
        b["match_extrait"] = extrait
        n_ann += 1

    if n_ann:
        vals = [b["match_qualitatif"] for b in biens if b.get("match_qualitatif") is not None]
        moy = round(sum(vals) / len(vals), 1) if vals else 0
        top = round(max(vals), 1) if vals else 0
        print(f"[Qualitatif] {n_ann}/{len(biens)} biens annotés "
              f"(similarité moy {moy} / max {top} sur 100)")
    return biens


# Test standalone : python workers/qualitative.py
if __name__ == "__main__":
    demo = [
        {"titre": "Belle longère en pierre pleine de cachet",
         "description": "Maison de caractère rénovée avec goût, poutres apparentes, "
                        "au calme à la campagne, lumineuse."},
        {"titre": "Appartement T3 récent",
         "description": "Immeuble neuf, balcon, proche tramway, prestations modernes."},
        {"titre": "Pavillon sans texte", "description": ""},
    ]
    crit = "maison ancienne de caractère en pierre, au calme, avec du charme et du cachet"
    annotate_biens(demo, crit)
    for b in demo:
        print(f"  {b['match_qualitatif']!s:>6}  | {b['titre'][:40]:40} | extrait: {b.get('match_extrait','')[:50]}")

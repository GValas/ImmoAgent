"""
qualitative.py — Évaluation LLM locale (Ollama) de la correspondance
description qualitative ↔ annonce.

Remplace l'ancien moteur d'embeddings (sentence-transformers) par un petit LLM
instruct servi par **Ollama** dans un conteneur dédié. Contrairement aux
embeddings, le LLM comprend la NÉGATION (« pas de », « non », « sans »), les
SEUILS NUMÉRIQUES (« piscine ≥ 4×9 m ») et pèse plusieurs critères à la fois.

Interface (inchangée pour analyst/scheduler), mais désormais ASYNCHRONE :
    async def annotate_biens(biens, description_qualitative) -> biens
NON destructif : annote chaque bien d'un
  • `match_qualitatif` : score 0–100 (None si l'annonce n'a pas de texte) ;
  • `match_extrait`    : justification courte du LLM (preuve lisible, colonne Excel).
Aucune élimination ici (tri par l'analyst) — les filtres durs restent
mots_interdits / critères structurés.

Configuration (variables d'environnement, posées par docker-compose) :
  • OLLAMA_HOST       : URL du serveur Ollama (défaut http://localhost:11434) ;
  • QUALITATIVE_MODEL : modèle Ollama (défaut qwen2.5:3b).

Dégradation gracieuse : si Ollama est injoignable ou le modèle absent, on
AVERTIT une fois et on no-op — le pipeline continue sans cette dimension, rien
n'est éliminé.
"""
import asyncio
import json
import os
import time

import httpx

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("QUALITATIVE_MODEL", "qwen2.5:3b")

# Nb d'appels Ollama simultanés. Le GPU sert les requêtes quasi en série ; un
# petit pipeline évite juste les temps morts réseau. Modeste pour ne pas saturer.
_CONCURRENCY = int(os.environ.get("QUALITATIVE_CONCURRENCY", "4"))
# Délai par appel (un 3B sur GPU répond en ~0,5–2 s ; on laisse de la marge).
_TIMEOUT = float(os.environ.get("QUALITATIVE_TIMEOUT", "60"))
# Budget temps GLOBAL (mur) pour toute la passe de scoring : si Ollama retombe
# silencieusement sur CPU, 400 biens × ~50 s ÷ 4 ≈ 90 min de blocage — passé ce
# budget, les biens restants sont laissés non annotés (non éliminatoire) et le
# pipeline continue. Surcharge : QUALITATIVE_BUDGET_S.
_BUDGET_S = float(os.environ.get("QUALITATIVE_BUDGET_S", "900"))

_SYSTEM_PROMPT = (
    "Tu es un assistant qui évalue dans quelle mesure une ANNONCE immobilière "
    "correspond à une DESCRIPTION DE RECHERCHE en français.\n"
    "Règles d'évaluation :\n"
    "- Respecte les NÉGATIONS : « pas de X », « non X », « sans X » signifient que X "
    "est INDÉSIRABLE → si l'annonce présente X, le score doit être BAS.\n"
    "- Respecte les SEUILS NUMÉRIQUES (surfaces, distances, dimensions).\n"
    "- N'INVENTE rien : si l'annonce ne mentionne pas un critère, ne le suppose ni "
    "présent ni absent — il est simplement neutre.\n"
    "- Pèse l'ensemble des critères ; un seul critère fort non respecté pénalise beaucoup.\n"
    "Réponds UNIQUEMENT par un objet JSON, sans texte autour. Donne D'ABORD la "
    "justification (ton analyse), PUIS le score qui en découle — dans cet ordre :\n"
    '{"justification": "<UNE seule phrase courte en français, ≤ 25 mots>", '
    '"score": <entier 0-100>}'
)

# Plafond de tokens générés : laisse au modèle la place de raisonner dans la
# justification (qui précède le score) tout en bornant la latence ; Ollama sérialise
# par défaut (NUM_PARALLEL=1), donc cette borne pèse directement sur le débit.
_NUM_PREDICT = int(os.environ.get("QUALITATIVE_NUM_PREDICT", "200"))


def _build_user_prompt(description_qualitative: str, titre: str, description: str) -> str:
    return (
        "DESCRIPTION DE RECHERCHE (critères du client) :\n"
        f"{description_qualitative.strip()}\n\n"
        "ANNONCE À ÉVALUER :\n"
        f"Titre : {titre.strip()}\n"
        f"Description : {description.strip()}\n\n"
        "Évalue la correspondance GLOBALE de l'annonce avec la description "
        "(0 = ne correspond pas du tout, 100 = correspond parfaitement)."
    )


async def _check_available(client: httpx.AsyncClient) -> bool:
    """Vérifie qu'Ollama répond et que le modèle est présent. N'écrit qu'un log."""
    try:
        r = await client.get(f"{OLLAMA_HOST}/api/tags", timeout=10)
        r.raise_for_status()
        tags = {m.get("name", "") for m in (r.json().get("models") or [])}
        # Correspondance EXACTE si MODEL porte un tag (qwen2.5:3b ≠ qwen2.5:7b —
        # l'ancien préfixe-match validait le mauvais tag puis chaque /api/chat
        # 404ait, un log d'erreur par bien). Sans tag explicite, tout tag du même
        # modèle convient (MODEL=qwen2.5 accepte qwen2.5:3b).
        if ":" in MODEL:
            present = MODEL in tags
        else:
            present = any(t == MODEL or t.split(":", 1)[0] == MODEL for t in tags)
        if not present:
            print(f"[Qualitatif] ⚠️  Modèle '{MODEL}' absent d'Ollama ({OLLAMA_HOST}). "
                  f"Modèles vus : {sorted(tags) or '∅'}. "
                  f"Vérifier le service 'ollama-pull' du docker-compose.")
            return False
        return True
    except Exception as e:
        print(f"[Qualitatif] ⚠️  Ollama injoignable ({OLLAMA_HOST}) : {type(e).__name__}: {e} "
              f"— dimension qualitative ignorée (rien n'est éliminé).")
        return False


def _coerce_score(value) -> float | None:
    try:
        s = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, s))


async def _score_one(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                     bien: dict, desc_qual: str,
                     deadline: float, budget_state: dict) -> None:
    """Annote un bien (in place). Best-effort : ne lève jamais."""
    texte_titre = bien.get("titre") or ""
    texte_desc = bien.get("description") or ""
    if len((texte_titre + texte_desc).strip()) < 12:
        bien["match_qualitatif"] = None
        bien["match_extrait"] = ""
        return

    # Budget temps global dépassé (ex. Ollama retombé sur CPU) → on laisse le bien
    # non annoté (non éliminatoire) au lieu de bloquer le cycle pendant des heures.
    if time.monotonic() >= deadline:
        if not budget_state["expired"]:
            budget_state["expired"] = True
            print(f"[Qualitatif] ⚠️  Budget temps ({_BUDGET_S:.0f}s) dépassé — "
                  f"scoring arrêté pour les biens restants (non éliminatoire)")
        bien.setdefault("match_qualitatif", None)
        bien.setdefault("match_extrait", "")
        return

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(desc_qual, texte_titre, texte_desc)},
        ],
        "stream": False,
        "format": "json",            # force une sortie JSON valide
        "options": {"temperature": 0, "num_predict": _NUM_PREDICT},
    }
    try:
        async with sem:
            # Le budget a pu expirer pendant l'attente du sémaphore.
            if time.monotonic() >= deadline:
                if not budget_state["expired"]:
                    budget_state["expired"] = True
                    print(f"[Qualitatif] ⚠️  Budget temps ({_BUDGET_S:.0f}s) dépassé — "
                          f"scoring arrêté pour les biens restants (non éliminatoire)")
                bien.setdefault("match_qualitatif", None)
                bien.setdefault("match_extrait", "")
                return
            r = await client.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=_TIMEOUT)
        r.raise_for_status()
        content = (r.json().get("message") or {}).get("content") or ""
        data = json.loads(content)
        score = _coerce_score(data.get("score"))
        justification = str(data.get("justification") or "").strip()
        bien["match_qualitatif"] = round(score, 1) if score is not None else None
        bien["match_extrait"] = justification
    except Exception as e:
        # Un échec ponctuel ne casse rien : bien laissé non annoté.
        bien.setdefault("match_qualitatif", None)
        bien.setdefault("match_extrait", "")
        print(f"[Qualitatif] échec scoring ({type(e).__name__}) sur "
              f"{(texte_titre or bien.get('url',''))[:50]}")


async def annotate_biens(biens: list[dict], description_qualitative: str) -> list[dict]:
    """Annote `match_qualitatif` (0–100) et `match_extrait` via le LLM Ollama.

    Idempotent et sûr : retourne `biens` inchangé si la description est vide,
    s'il n'y a aucun bien, ou si Ollama / le modèle sont indisponibles."""
    desc = (description_qualitative or "").strip()
    if not desc or not biens:
        return biens

    async with httpx.AsyncClient() as client:
        if not await _check_available(client):
            return biens

        print(f"[Qualitatif] Évaluation LLM ({MODEL} via {OLLAMA_HOST}) "
              f"sur {len(biens)} biens…")
        sem = asyncio.Semaphore(_CONCURRENCY)
        deadline = time.monotonic() + _BUDGET_S
        budget_state = {"expired": False}
        await asyncio.gather(*[_score_one(client, sem, b, desc, deadline, budget_state)
                               for b in biens])

    vals = [b["match_qualitatif"] for b in biens if b.get("match_qualitatif") is not None]
    if vals:
        moy = round(sum(vals) / len(vals), 1)
        top = round(max(vals), 1)
        print(f"[Qualitatif] {len(vals)}/{len(biens)} biens annotés "
              f"(score moy {moy} / max {top} sur 100)")
    return biens


# Test standalone : OLLAMA_HOST=http://localhost:11434 python workers/qualitative.py
if __name__ == "__main__":
    demo = [
        {"titre": "Belle longère en pierre pleine de cachet",
         "description": "Maison ancienne de caractère rénovée avec goût, pierres "
                        "apparentes et colombages, au calme à la campagne, piscine "
                        "extérieure 5x10, proche commerces."},
        {"titre": "Villa contemporaine d'architecte",
         "description": "Maison contemporaine de 2015, lignes épurées, dans un "
                        "lotissement récent, zone inondable, gros travaux à prévoir."},
        {"titre": "Pavillon sans texte", "description": ""},
    ]
    crit = ("- Champêtre, caractère, authentique\n"
            "- Matériaux : pierres apparentes, colombages\n"
            "- pas de zone inondable\n"
            "- pas de travaux à prévoir\n"
            "- pas de maison contemporaine")
    asyncio.run(annotate_biens(demo, crit))
    for b in demo:
        print(f"  {b.get('match_qualitatif')!s:>6}  | {b['titre'][:40]:40} | "
              f"{b.get('match_extrait','')[:60]}")

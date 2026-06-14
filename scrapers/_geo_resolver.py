"""scrapers/_geo_resolver.py — Résolveur commune → (département, code postal).

Certaines petites agences n'exposent ni code postal ni param département dans
leurs cartes de liste : seul le NOM de commune est disponible (titre/slug). Pour
garantir 0 fuite hors-département, ce module résout chaque nom de commune en
(code_departement, code_postal) via l'API publique geo.api.gouv.fr.

Caractéristiques :
  - async (httpx), cache mémoire process partagé entre appels d'un même run ;
  - on restreint les candidats aux départements cibles passés en argument, ce qui
    lève l'ambiguïté des homonymes (ex. « Clamecy » -> 58 et non 02) ;
  - dédoublonnage : on ne fait qu'UN appel réseau par nom de commune distinct ;
  - best-effort : si l'API est injoignable, retourne (None, None) -> le bien sera
    écarté par le post-filtre département strict (prudence > fuite).

Interface :
    await resolve_communes(noms, departements_cibles) -> dict[nom -> (dept, cp)]
    await resolve_one(client, nom, depts_set) -> (dept, cp) | (None, None)
"""
from __future__ import annotations

import asyncio
import re
import unicodedata
from typing import Optional

from scrapers._base import make_client

_GEO_URL = "https://geo.api.gouv.fr/communes"
_cache: dict[str, tuple[Optional[str], Optional[str]]] = {}


def _norm(nom: str) -> str:
    """Normalise un nom de commune pour la clé de cache (sans accents, minuscules)."""
    nom = unicodedata.normalize("NFKD", nom or "")
    nom = "".join(c for c in nom if not unicodedata.combining(c))
    nom = re.sub(r"\s+", " ", nom).strip().lower()
    # retire suffixes parasites fréquents dans les titres/slugs
    nom = re.sub(r"\b(centre|centre-ville|ville|secteur|proche|environs?)\b", "", nom)
    return re.sub(r"\s+", " ", nom).strip(" -")


async def resolve_one(
    client, nom: str, depts: set[str]
) -> tuple[Optional[str], Optional[str]]:
    """Résout un nom de commune en (dept, cp), restreint aux départements `depts`."""
    key = _norm(nom)
    if not key:
        return (None, None)
    if key in _cache:
        return _cache[key]

    result: tuple[Optional[str], Optional[str]] = (None, None)
    try:
        r = await client.get(
            _GEO_URL,
            params={
                "nom": key,
                "fields": "codeDepartement,codesPostaux,nom",
                "boost": "population",
                "limit": 8,
            },
        )
        if r.status_code == 200:
            data = r.json()
            # 1) priorité : une commune dans un département cible
            for c in data:
                if c.get("codeDepartement") in depts:
                    cps = c.get("codesPostaux") or []
                    result = (c["codeDepartement"], cps[0] if cps else None)
                    break
    except Exception:
        result = (None, None)

    _cache[key] = result
    return result


async def resolve_communes(
    noms, departements_cibles
) -> dict[str, tuple[Optional[str], Optional[str]]]:
    """Résout en lot une liste de noms de communes (dédoublonnés).

    Retourne {nom_normalisé -> (dept, cp)}. Concurrence bornée pour rester poli.
    """
    depts = {str(d).zfill(2) for d in departements_cibles}
    uniques = sorted({_norm(n) for n in noms if _norm(n)})
    out: dict[str, tuple[Optional[str], Optional[str]]] = {}
    sem = asyncio.Semaphore(5)

    async with make_client(timeout=15) as client:

        async def _do(name: str):
            async with sem:
                out[name] = await resolve_one(client, name, depts)

        await asyncio.gather(*(_do(n) for n in uniques))

    return out

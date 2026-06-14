"""scrapers/_geo_resolve.py — Résolution nom de commune → (département, CP).

Plusieurs agences locales (CMS AC3 ancienne variante, WordPress RealHomes,
sites « cartes sans code postal »…) n'exposent PAS le code postal sur la carte de
liste : seul le NOM DE COMMUNE est disponible. Pour garantir « 0 fuite hors-zone »
on résout ce nom en (codeDepartement, codePostal) via l'API officielle
geo.api.gouv.fr (gratuite, sans clé), avec match exact de nom prioritaire et
cache mémoire pour limiter les requêtes.

Repris du pattern éprouvé de scrapers/cabinet_girard.py, factorisé ici pour les
nouveaux scrapers scraper-scout (maintenon/chaumiere/apally variante sans CP,
bannier, girard, tradim…).
"""
from __future__ import annotations

import unicodedata

import httpx

GEO_API = "https://geo.api.gouv.fr/communes"


def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text or "")
        if unicodedata.category(c) != "Mn"
    )


async def resolve_dept(
    client: httpx.AsyncClient, ville: str, cache: dict[str, tuple[str, str]],
) -> tuple[str, str]:
    """Nom de commune → (codeDepartement, codePostal) via geo.api.gouv.fr.

    Match exact de nom prioritaire (insensible casse/accents), repli sur le 1er
    résultat trié par population. Renvoie ("", "") si non résolu. `cache` est un
    dict partagé entre appels (clé = nom normalisé)."""
    key = strip_accents(ville).lower().strip()
    if not key:
        return "", ""
    if key in cache:
        return cache[key]

    res: tuple[str, str] = ("", "")
    try:
        r = await client.get(
            GEO_API,
            params={
                "nom": ville,
                "fields": "nom,codesPostaux,codeDepartement",
                "boost": "population",
                "limit": 5,
            },
        )
        if r.status_code == 200:
            data = r.json()
            chosen = None
            for c in data:
                if strip_accents(c.get("nom", "")).lower() == key:
                    chosen = c
                    break
            if chosen is None and data:
                chosen = data[0]
            if chosen:
                dept = chosen.get("codeDepartement", "") or ""
                cps = chosen.get("codesPostaux", []) or []
                cp = cps[0] if cps else (dept + "000" if dept else "")
                res = (dept, cp)
    except Exception:
        res = ("", "")

    cache[key] = res
    return res

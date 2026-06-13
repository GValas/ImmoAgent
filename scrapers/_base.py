"""scrapers/_base.py — Socle commun des scrapers SSR (httpx + BeautifulSoup).

Le parc de ~277 scrapers recopiait le même gabarit : bloc HEADERS (291 copies
verbatim), map département→slug (62 copies identiques), boucle dept + pagination
(206 copies), helpers de parsing prix/surface/terrain (jusqu'à 194 copies
byte-for-byte). Ce module factorise tout ça :

  - HEADERS / DEFAULT_DEPT_SLUGS : constantes partagées ;
  - make_client / get_with_retry : client httpx + retry 429/503 (jadis seulement
    dans gallery.py) ;
  - parse_price / parse_int / parse_terrain / parse_surface / parse_loc : helpers ;
  - run_dept_search : driver générique « boucle départements + pagination + filtres
    prix/surface + dédup id » — un scraper SSR se réduit alors à : un sélecteur de
    carte, une fonction parse_card et un patron d'URL ;
  - standalone_main : bloc CLI `python scrapers/xxx.py`.

Interface inchangée pour le pipeline : chaque scraper expose toujours
`async def search(criteres: dict) -> list[dict]`.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Optional

import httpx
from bs4 import BeautifulSoup

# User-Agent navigateur standard (auparavant recopié dans ~291 scrapers).
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Code département → slug standard « nom-de-departement » (les 11 départements
# cibles). Réutilisable tel quel ou via override par site (slugs spécifiques).
DEFAULT_DEPT_SLUGS: dict[str, str] = {
    "72": "sarthe",
    "28": "eure-et-loir",
    "45": "loiret",
    "89": "yonne",
    "49": "maine-et-loire",
    "37": "indre-et-loire",
    "36": "indre",
    "18": "cher",
    "58": "nievre",
    "41": "loir-et-cher",
    "53": "mayenne",
}


# ── Client HTTP + retry ────────────────────────────────────────────────────────

def make_client(timeout: float = 20, **kwargs) -> httpx.AsyncClient:
    """Client httpx avec HEADERS + follow_redirects par défaut."""
    kwargs.setdefault("headers", HEADERS)
    kwargs.setdefault("follow_redirects", True)
    return httpx.AsyncClient(timeout=timeout, **kwargs)


async def get_with_retry(
    client: httpx.AsyncClient, url: str, *, retries: int = 2, backoff: float = 1.5,
    **kwargs,
) -> Optional[httpx.Response]:
    """GET avec retry/backoff sur 429/503 (anti-throttle). Retourne la réponse
    (même non-200) ou None si erreur réseau persistante. Jadis cette logique
    n'existait que dans gallery.py ; les scrapers faisaient juste `break`."""
    for attempt in range(retries + 1):
        try:
            r = await client.get(url, **kwargs)
        except Exception:
            if attempt == retries:
                return None
            await asyncio.sleep(backoff * (attempt + 1))
            continue
        if r.status_code in (429, 503) and attempt < retries:
            await asyncio.sleep(backoff * (attempt + 1))
            continue
        return r
    return None


# ── Helpers de parsing (versions canoniques, ex-le_tuc.py) ───────────────────────

def parse_price(text: str) -> Optional[float]:
    """'139 000 €' → 139000.0 ; '' → None."""
    cleaned = re.sub(r"[€\s\xa0]", "", text or "").replace(",", ".")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def parse_int(pattern: str, text: str) -> Optional[int]:
    """Premier entier capturé par `pattern` (groupe 1) dans `text`, sinon None."""
    m = re.search(pattern, text or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


def parse_terrain(text: str) -> Optional[float]:
    """'Superficie terrain en m² 2135 m²' → 2135.0."""
    m = re.search(r"Superficie\s+terrain[^0-9]*([\d\s\xa0]+)\s*m", text or "", re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            return float(val)
        except ValueError:
            pass
    return None


def parse_surface(text: str, lo: float = 8, hi: float = 2000) -> Optional[float]:
    """Surface habitable 'NNN m² hab / de surface / habitable' dans du texte libre,
    bornée [lo, hi]. None si rien de plausible."""
    if not text:
        return None
    m = re.search(r"(\d[\d\s\xa0]*)\s*m²?\s*(?:hab|de surface|habitable)", text, re.IGNORECASE)
    if m:
        val = re.sub(r"[\s\xa0]", "", m.group(1))
        try:
            f = float(val)
            if lo <= f <= hi:
                return f
        except ValueError:
            pass
    return None


def parse_loc(text: str) -> tuple[str, str]:
    """'Saint-Loup-des-Vignes (45340)' → ('Saint-Loup-des-Vignes', '45340')."""
    cp = ""
    m_cp = re.search(r"\((\d{5})\)", text or "")
    if m_cp:
        cp = m_cp.group(1)
    ville = re.sub(r"\s*\(\d{5}\)\s*$", "", text or "").strip()
    return ville, cp


# ── Driver générique département + pagination ────────────────────────────────────

async def run_dept_search(
    *,
    source: str,
    page_url: Callable[[str, str, int], str],
    card_selector: str,
    parse_card: Callable[[object, str], Optional[dict]],
    criteres: dict,
    dept_slugs: dict[str, str] = DEFAULT_DEPT_SLUGS,
    max_pages: int = 8,
    page_sleep: float = 0.5,
    dept_sleep: float = 0.6,
    label: Optional[str] = None,
) -> list[dict]:
    """Boucle sur les départements cibles puis pagine chaque liste SSR.

    `page_url(dept, slug, page)` construit l'URL de liste ; `parse_card(card, dept)`
    transforme une carte BeautifulSoup en dict bien (ou None pour l'ignorer).
    Applique : arrêt sur non-200 / page vide / 0 nouveau, dédup par id_annonce,
    garde-fou département (préfixe code_postal), filtres prix_min/max & surface_min.
    Retourne la liste agrégée."""
    label = label or source
    departements = [str(d).zfill(2) for d in criteres.get("departements", [])]
    prix_max = criteres.get("prix_max", 0)
    prix_min = criteres.get("prix_min", 0)
    surface_min = criteres.get("surface_min", 0)
    results: list[dict] = []

    async with make_client() as client:
        for dept in departements:
            slug = dept_slugs.get(dept)
            if not slug:
                continue
            try:
                biens = await _scrape_one_dept(
                    client, dept, slug, page_url, card_selector, parse_card,
                    prix_max, prix_min, surface_min, max_pages, page_sleep,
                )
                results.extend(biens)
                print(f"[{label}] Dept {dept}: {len(biens)} annonces")
            except Exception as e:
                print(f"[{label}] Erreur dept {dept}: {e}")
            await asyncio.sleep(dept_sleep)

    return results


async def _scrape_one_dept(
    client, dept, slug, page_url, card_selector, parse_card,
    prix_max, prix_min, surface_min, max_pages, page_sleep,
) -> list[dict]:
    biens: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, max_pages + 1):
        r = await get_with_retry(client, page_url(dept, slug, page))
        if r is None or r.status_code != 200:
            break
        cards = BeautifulSoup(r.text, "html.parser").select(card_selector)
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            try:
                bien = parse_card(card, dept)
            except Exception:
                continue
            if not bien:
                continue
            cp = str(bien.get("code_postal") or "")
            if cp and cp[:2] != dept:
                continue
            aid = bien.get("id_annonce") or bien.get("url")
            if aid in seen_ids:
                continue
            p = bien.get("prix") or 0
            s = bien.get("surface") or 0
            if prix_max and p and p > prix_max:
                continue
            if prix_min and p and p < prix_min:
                continue
            if surface_min and s and s < surface_min:
                continue
            seen_ids.add(aid)
            biens.append(bien)
            new_on_page += 1

        if new_on_page == 0:
            break
        await asyncio.sleep(page_sleep)

    return biens


# ── CLI standalone (python scrapers/xxx.py) ──────────────────────────────────────

def standalone_main(search: Callable[[dict], Awaitable[list[dict]]], label: str) -> None:
    """Bloc `__main__` partagé : charge criteria.md, lance search(), résume."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from config_loader import load_criteria

    criteres = load_criteria()
    biens = asyncio.run(search({
        "departements": criteres.departements,
        "prix_max": criteres.prix_max,
        "prix_min": getattr(criteres, "prix_min", 0),
        "surface_min": criteres.surface_min,
    }))
    print(f"\nTotal {label}: {len(biens)} annonces")
    depts = sorted({str(b.get("code_postal") or "")[:2] for b in biens if b.get("code_postal")})
    print(f"Départements vus : {depts}")
    for b in biens[:10]:
        print(f"  [{b.get('code_postal')}] {str(b.get('titre'))[:55]} — "
              f"{b.get('prix')}€ — {b.get('surface') or '?'}m² — {b.get('ville')}")

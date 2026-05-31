"""
scrapers/gallery.py — Récupération de la galerie photo COMPLÈTE d'une annonce.

La plupart des scrapers ne captent que 0-1 photo en vue liste. Ce module va
chercher TOUTES les photos sur la page détail (ou l'API) de l'annonce, pour les
seuls biens survivants (appelé tardivement dans le pipeline, pas pour tous les biens).

Interface :
    async def fetch_gallery(bien: dict, session: httpx.AsyncClient) -> list[str]

Garanties :
    - Ne lève JAMAIS d'exception. Toute erreur réseau/parsing → on retourne
      `bien.get('photos') or []` (on ne casse rien dans le pipeline).
    - Dispatch par `bien['source']` ; fallback générique (JSON-LD / og:image /
      <img>/srcset/data-*) pour les sources non spécialisées.
    - Si la galerie récupérée contient MOINS de photos que `bien['photos']`
      (déjà connues), on retourne l'existant.
    - httpx async pur (réutilise la `session` passée). Pas de Playwright : si une
      source charge ses photos uniquement en JS sans API/JSON-LD/SSR, on retourne
      l'existant et c'est noté dans le tableau de couverture.

Conventions du repo : async/await, logs via print(f"[Gallery] ...").
"""
from __future__ import annotations

import re
import html as _html
from urllib.parse import urljoin, urlparse, parse_qs

import httpx

try:  # exécution comme module du package
    import json as _json
except Exception:  # pragma: no cover
    _json = None


# --------------------------------------------------------------------------- #
# Filtres communs
# --------------------------------------------------------------------------- #

# Fragments d'URL qui ne sont jamais des photos d'annonce.
_BLACKLIST_FRAGMENTS = (
    "logo", "logos", "/logo", "picto", "pictos", "avatar", "icon", "/icons",
    "sprite", "placeholder", "novisu", "no-visu", "no_photo", "nophoto",
    "default", "blank", "1px", "spacer", "transparent", "loader", "loading",
    "/static/", "/_static_/", "/assets/img/", "diagrammeenergie", "/dpe.",
    "/dpe/", "diagnostic", "watermark", "facebook", "twitter", "instagram",
    "youtube", "linkedin", "flag-", "/flags/", "profile-picture", "agence-logo",
    "mandataires", "bandeau", "header", "footer", "carte", "/map", "google",
    "/share/", "media-logo", "/logo-",
)

# Extensions d'image acceptées (certaines sources servent sans extension : voir
# les fetchers dédiés qui contournent ce filtre).
_IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp)(?:[?#].*)?$", re.IGNORECASE)


def _looks_like_photo(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    low = url.lower()
    if any(frag in low for frag in _BLACKLIST_FRAGMENTS):
        return False
    return True


def _dedup_keep_order(urls: list[str]) -> list[str]:
    seen = set()
    out = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _clean(url: str) -> str:
    """Décode les entités HTML (&amp;) éventuelles."""
    return _html.unescape(url.strip())


def _better_of(existing: list[str], found: list[str], bien: dict) -> list[str]:
    """Retourne `found` si strictement plus riche que l'existant, sinon l'existant."""
    existing = existing or (bien.get("photos") or [])
    found = _dedup_keep_order([_clean(u) for u in found if _looks_like_photo(_clean(u))])
    if len(found) > len(existing):
        return found
    return existing


# --------------------------------------------------------------------------- #
# Fallback générique (SSR / JSON-LD / og:image / <img>/srcset/data-*)
# --------------------------------------------------------------------------- #

_OG_RE = re.compile(
    r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_RE2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    re.IGNORECASE,
)
_LDJSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(
    r'(?:src|data-src|data-original|data-lazy|data-lazy-src|data-bg|data-image|data-full|data-large)=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_SRCSET_RE = re.compile(r'(?:srcset|data-srcset)=["\']([^"\']+)["\']', re.IGNORECASE)


def _extract_ldjson_images(txt: str, base: str) -> list[str]:
    out: list[str] = []
    for block in _LDJSON_RE.findall(txt):
        block = block.strip()
        if '"image"' not in block and "'image'" not in block:
            continue
        try:
            data = _json.loads(block)
        except Exception:
            # Parsing brut si JSON invalide : on capture les valeurs d'image au regex.
            for m in re.findall(r'"image"\s*:\s*(\[[^\]]*\]|"[^"]+")', block):
                for u in re.findall(r'https?://[^"\'\\ ]+', m):
                    out.append(u)
            continue

        def _collect(node):
            if isinstance(node, dict):
                img = node.get("image")
                if img:
                    _collect_img(img)
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        _collect(v)
            elif isinstance(node, list):
                for v in node:
                    _collect(v)

        def _collect_img(img):
            if isinstance(img, str):
                out.append(img)
            elif isinstance(img, list):
                for it in img:
                    _collect_img(it)
            elif isinstance(img, dict):
                u = img.get("url") or img.get("contentUrl")
                if u:
                    out.append(u)

        _collect(data)
    return [urljoin(base, _clean(u)) for u in out]


def _srcset_best(value: str) -> str | None:
    """Retourne l'URL de plus grande largeur d'un attribut srcset."""
    best = None
    best_w = -1
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        url = bits[0]
        w = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            try:
                w = int(bits[1][:-1])
            except ValueError:
                w = 0
        if w >= best_w:
            best_w = w
            best = url
    return best


def _extract_generic(txt: str, base: str) -> list[str]:
    urls: list[str] = []

    # 1) JSON-LD (souvent la galerie complète)
    urls += _extract_ldjson_images(txt, base)

    # 2) og:image
    for m in _OG_RE.findall(txt) + _OG_RE2.findall(txt):
        urls.append(urljoin(base, _clean(m)))

    # 3) <img> : src / data-src / srcset
    for tag in _IMG_TAG_RE.findall(txt):
        ss = _SRCSET_RE.search(tag)
        if ss:
            best = _srcset_best(_clean(ss.group(1)))
            if best:
                urls.append(urljoin(base, _clean(best)))
        for attr_url in _ATTR_RE.findall(tag):
            urls.append(urljoin(base, _clean(attr_url)))

    # On ne garde que ce qui ressemble à une image (extension) et n'est pas blacklisté.
    out = []
    for u in urls:
        cu = _clean(u)
        if _IMG_EXT_RE.search(cu) and _looks_like_photo(cu):
            out.append(cu)
    return _dedup_by_stem(out)


def _dedup_by_stem(urls: list[str]) -> list[str]:
    """Dédup en collapsant les variantes de taille d'une même photo.

    Beaucoup de sites servent la même image via un redimensionneur (glide, ?w=...,
    /thumb/, hôtes CDN différents) → on regroupe par nom de fichier (stem) et on
    garde la 1re occurrence (souvent la plus grande/canonique). Conserve l'ordre.
    """
    seen_full = set()
    seen_stem = set()
    out = []
    for u in urls:
        if u in seen_full:
            continue
        seen_full.add(u)
        path = urlparse(u).path
        stem = path.rsplit("/", 1)[-1].lower()  # ex: 52379349a.jpg
        # On garde le 1er représentant de chaque nom de fichier.
        if stem and stem in seen_stem:
            continue
        seen_stem.add(stem)
        out.append(u)
    return out


async def _get(session: httpx.AsyncClient, url: str, **kw):
    """GET avec retry/backoff sur 429/503 (sites publics rate-limités, ex. century21)."""
    import asyncio
    r = None
    for attempt in range(3):
        r = await session.get(url, follow_redirects=True, timeout=25, **kw)
        if r.status_code in (429, 503) and attempt < 2:
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
        break
    r.raise_for_status()
    return r


async def _get_text(session: httpx.AsyncClient, url: str, **kw) -> str:
    r = await _get(session, url, **kw)
    return r.text


async def _get_json(session: httpx.AsyncClient, url: str, headers: dict | None = None) -> dict:
    h = {"Accept": "application/json"}
    if headers:
        h.update(headers)
    r = await _get(session, url, headers=h)
    return r.json()


# --------------------------------------------------------------------------- #
# Fetchers dédiés par source
# --------------------------------------------------------------------------- #

async def _g_notaires(bien: dict, session: httpx.AsyncClient) -> list[str]:
    """immobilier_notaires & notaires_valdeloire — API officielle /v1/annonces/{id}.

    Galerie complète dans vente.multimedias[].urlHighestResolution.
    """
    url = bien.get("url", "")
    aid = bien.get("id_annonce") or ""
    if not aid:
        m = re.search(r"/(\d{5,})(?:[/?#].*)?$", url)
        if m:
            aid = m.group(1)
    if not aid:
        return []
    api = f"https://www.immobilier.notaires.fr/pub-services/inotr-www-annonces/v1/annonces/{aid}"
    data = await _get_json(session, api, headers={
        "Accept": "application/json",
        "Referer": "https://www.immobilier.notaires.fr/",
    })
    mm = (((data or {}).get("vente") or {}).get("multimedias")) or []
    out = []
    for m in mm:
        if not isinstance(m, dict):
            continue
        if str(m.get("type", "")).startswith("video"):
            continue
        u = m.get("urlHighestResolution") or (m.get("xga") or {}).get("url") or (m.get("vga") or {}).get("url")
        if u:
            out.append(u)
    return out


async def _g_iad(bien: dict, session: httpx.AsyncClient) -> list[str]:
    """iad — LIMITATION : la page détail est une coquille SSR sans les vraies photos.

    iadfrance.fr charge la galerie côté JS (l'API détail exige la référence interne
    du bien, pas le slug d'URL). Le SSR ne contient qu'une image placeholder
    (product-1988031-*), identique pour toutes les annonces → inexploitable.
    Ce fetcher ne renverra donc en pratique rien d'utile ; fetch_gallery retombera
    sur bien['photos'] (la photo de liste). Pour la galerie iad, il faudrait soit
    conserver `item['photos']` au moment du scrape liste (iad.py), soit Playwright.

    On tente quand même l'extraction au cas où iad servirait un jour les vraies
    photos en SSR (width=300/600/900... par photo → on garde la plus large).
    """
    txt = await _get_text(session, bien["url"])
    # Les photos 2..N sont dans un îlot JSON Next.js avec slashes échappés (/)
    # et l'hôte iadfrance.com → on déséchappe avant extraction.
    txt = txt.replace("\\u002F", "/").replace("\\u002f", "/")
    raw = re.findall(
        r"https?://images\.iadfrance\.(?:fr|com)/photos/realestate/[^\"'\\ ]+?\.(?:jpe?g|png|webp)[^\"'\\ ]*",
        txt,
    )
    best: dict[str, tuple[int, str]] = {}
    for u in raw:
        u = _clean(u)
        # Clé de dédup : nom de fichier (product-{id}-{n}.jpg), indépendant de
        # l'hôte (.fr/.com) et de la query (?ts=...&width=...).
        base = u.split("?")[0].rsplit("/", 1)[-1]
        w = 0
        qs = parse_qs(urlparse(u).query)
        if "width" in qs:
            try:
                w = int(qs["width"][0])
            except (ValueError, IndexError):
                w = 0
        if base not in best or w > best[base][0]:
            best[base] = (w, u)
    # Tri par numéro de photo pour conserver l'ordre de la galerie.
    def _photo_idx(item):
        m = re.search(r"-(\d+)\.(?:jpe?g|png|webp)", item[0])
        return int(m.group(1)) if m else 0
    return [v[1] for _, v in sorted(best.items(), key=_photo_idx)]


async def _g_century21(bien: dict, session: httpx.AsyncClient) -> list[str]:
    """century21 — galerie en SSR (relatif /imagesBien/...).

    Chaque photo existe en plusieurs tailles : .../c21_..._{IDX}_{GUID}.jpg
    où IDX est l'index de taille (8 = grand, 1 = petit). On dédup par GUID et
    on garde la taille 8.
    """
    txt = await _get_text(session, bien["url"])
    paths = re.findall(r"/imagesBien/[A-Za-z0-9_/.\-]+\.(?:jpe?g|png|webp)", txt)
    by_guid: dict[str, tuple[int, str]] = {}
    for p in paths:
        full = urljoin("https://www.century21.fr", p)
        guid = p.split("_")[-1]            # {GUID}.jpg
        try:
            idx = int(p.split("_")[-2])    # index de taille
        except (ValueError, IndexError):
            idx = 0
        if guid not in by_guid or idx > by_guid[guid][0]:
            by_guid[guid] = (idx, full)
    return [v[1] for v in by_guid.values()]


async def _g_proprietes_privees(bien: dict, session: httpx.AsyncClient) -> list[str]:
    """proprietes_privees — photos séquentielles PROPRIETES-PRIVEES-{ref}-N.jpg."""
    txt = await _get_text(session, bien["url"])
    raw = re.findall(
        r"https://images\.proprietes-privees\.com/annonce/[^\"'\\ ]+?\.(?:jpe?g|png|webp)",
        txt,
    )
    return [_clean(u) for u in raw]


async def _g_lesiteimmo(bien: dict, session: httpx.AsyncClient) -> list[str]:
    """lesiteimmo — photos sur media.studio-net.fr/biens/{id}/{hash} (SANS extension).

    On restreint à l'id du bien pour éviter les photos d'autres annonces de la page.
    """
    txt = await _get_text(session, bien["url"])
    aid = bien.get("id_annonce") or ""
    if not aid:
        m = re.search(r"/(\d{5,})(?:[/?#].*)?$", bien.get("url", ""))
        aid = m.group(1) if m else ""
    if aid:
        pat = rf"https://media\.studio-net\.fr/biens/{re.escape(aid)}/[A-Za-z0-9]+"
    else:
        pat = r"https://media\.studio-net\.fr/biens/\d+/[A-Za-z0-9]+"
    raw = re.findall(pat, txt)
    # On retire le suffixe de query (?w=...) pour la version originale.
    return _dedup_keep_order([_clean(u) for u in raw])


async def _g_foncia(bien: dict, session: httpx.AsyncClient) -> list[str]:
    """foncia — photos cloudfront, mêmes hash en lg/md/sm → on garde lg."""
    txt = await _get_text(session, bien["url"])
    raw = re.findall(
        r"https://d7b3sch6x3cpd\.cloudfront\.net/annonces/[A-Za-z0-9/]+/(?:lg|md|sm)\.(?:jpe?g|png|webp)",
        txt,
    )
    by_path: dict[str, str] = {}
    rank = {"lg": 3, "md": 2, "sm": 1}
    for u in raw:
        u = _clean(u)
        base = u.rsplit("/", 1)[0]              # chemin sans le suffixe de taille
        size = u.rsplit("/", 1)[1].split(".")[0]
        if base not in by_path or rank.get(size, 0) > rank.get(by_path[base].rsplit("/", 1)[1].split(".")[0], 0):
            by_path[base] = u
    return list(by_path.values())


async def _g_drhouse(bien: dict, session: httpx.AsyncClient) -> list[str]:
    """drhouse — galerie minisite/detail/.../vente/{listingid}/{n}.webp.

    (les images bandeau/mandataires sont des bannières d'agent → filtrées.)
    """
    txt = await _get_text(session, bien["url"])
    raw = re.findall(
        r"https://images\.drhouse-immo\.com/minisite/detail/[A-Za-z0-9/.\-]+?\.(?:webp|jpe?g|png)",
        txt,
    )
    return _dedup_keep_order([_clean(u) for u in raw])


async def _g_fnaim(bien: dict, session: httpx.AsyncClient) -> list[str]:
    """fnaim — photos sur imagesv2.fnaim.fr/images1/img/... (logos/ filtrés)."""
    txt = await _get_text(session, bien["url"])
    raw = re.findall(
        r"https://imagesv2\.fnaim\.fr/images\d*/img/[^\"'\\ ]+?\.(?:jpe?g|png|webp)",
        txt,
    )
    return _dedup_keep_order([_clean(u) for u in raw])


async def _g_paruvendu(bien: dict, session: httpx.AsyncClient) -> list[str]:
    """paruvendu — photos via img.paruvendu.fr/media_ext/.../...?func=crop&w=1000.

    Les vignettes des annonces liées sont en w=480 → on privilégie w=1000 et on
    filtre les logos. Beaucoup d'annonces paruvendu n'ont réellement aucune photo
    (placeholder novisu) → on retourne alors l'existant.
    """
    txt = await _get_text(session, bien["url"])
    raw = re.findall(r"https://[a-z0-9.\-]*paruvendu\.fr/media_ext/[^\"'\\ )]+", txt)
    big, small = [], []
    for u in raw:
        u = _clean(u)
        low = u.lower()
        if "logo" in low or "media-logo" in low:
            continue
        if "func=crop" not in low and "w=" not in low:
            continue
        if "w=1000" in low:
            big.append(u)
        else:
            small.append(u)
    photos = big if big else small
    return _dedup_keep_order(photos)


# Sources où la page détail est JS-only (le fallback générique ne renvoie que du
# chrome/placeholder) → on NE tente PAS le générique, on garde bien['photos'].
_JS_ONLY = {"iad"}

# Mapping source → fetcher dédié. Les sources absentes passent par le fallback.
_DISPATCH = {
    "immobilier_notaires": _g_notaires,
    "notaires_valdeloire": _g_notaires,
    "iad": _g_iad,
    "century21": _g_century21,
    "proprietes_privees": _g_proprietes_privees,
    "lesiteimmo": _g_lesiteimmo,
    "foncia": _g_foncia,
    "drhouse": _g_drhouse,
    "fnaim": _g_fnaim,
    "paruvendu": _g_paruvendu,
}


# --------------------------------------------------------------------------- #
# Interface publique
# --------------------------------------------------------------------------- #

async def fetch_gallery(bien: dict, session: httpx.AsyncClient) -> list[str]:
    """Retourne la liste des URLs photo de l'annonce (page détail), [] si échec.

    Ne lève JAMAIS d'exception. Si on récupère moins que `bien['photos']`, on
    retourne l'existant.
    """
    existing = bien.get("photos") or []
    url = bien.get("url", "")
    if not url or not url.startswith("http"):
        return existing

    source = (bien.get("source") or "").strip()
    fetcher = _DISPATCH.get(source)

    # 1) Fetcher dédié
    if fetcher is not None:
        try:
            found = await fetcher(bien, session)
            result = _better_of(existing, found, bien)
            if len(result) > len(existing):
                return result
            # Le fetcher dédié n'a rien donné de mieux → on tente le générique,
            # sauf pour les sources JS-only (générique = chrome/placeholder).
        except Exception as e:
            print(f"[Gallery] {source} fetcher KO ({url[:60]}): {e}")

    if source in _JS_ONLY:
        return existing

    # 2) Fallback générique (SSR / JSON-LD / og:image / <img>)
    try:
        txt = await _get_text(session, url)
        base = url
        found = _extract_generic(txt, base)
        return _better_of(existing, found, bien)
    except Exception as e:
        print(f"[Gallery] generic KO {source} ({url[:60]}): {e}")
        return existing


# --------------------------------------------------------------------------- #
# Test manuel : python scrapers/gallery.py
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import asyncio
    import glob
    import json
    import os
    import sys
    import statistics

    sys.stdout.reconfigure(encoding="utf-8")

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9",
    }

    HERE = os.path.dirname(os.path.abspath(__file__))
    RAW = os.path.join(HERE, "..", "data", "raw")

    # Sources à tester en priorité + quelques-unes en fallback générique.
    TARGET = [
        "iad", "immobilier_notaires", "notaires_valdeloire", "century21",
        "lesiteimmo", "paruvendu", "proprietes_privees", "fnaim", "drhouse",
        "foncia",
        # sources passant par le fallback générique :
        "era", "greenacres", "cimm", "meilleursbiens", "noovimo", "nicole_joubert",
        "laforet", "arthurimmo", "webimmo123", "clefrance",
    ]
    PER_SOURCE = 3

    async def _test():
        files = sorted(glob.glob(os.path.join(RAW, "biens_prevision_*.json")))
        if not files:
            print("Aucun fichier biens_prevision_*.json dans data/raw")
            return
        data = json.load(open(files[-1], encoding="utf-8"))
        print(f"Source de test : {os.path.basename(files[-1])} ({len(data)} biens)\n")

        # Échantillon par source
        buckets: dict[str, list[dict]] = {}
        for b in data:
            s = b.get("source")
            if s in TARGET and len(buckets.setdefault(s, [])) < PER_SOURCE:
                buckets[s].append(b)

        rows = []
        async with httpx.AsyncClient(headers=_HEADERS, timeout=30) as session:
            for s in TARGET:
                biens = buckets.get(s, [])
                if not biens:
                    rows.append((s, "—", "—", "pas d'échantillon dans le dernier run"))
                    continue
                liste_n, galerie_n = [], []
                detail = []
                for b in biens:
                    n_before = len(b.get("photos") or [])
                    photos = await fetch_gallery(b, session)
                    n_after = len(photos)
                    liste_n.append(n_before)
                    galerie_n.append(n_after)
                    detail.append(f"{n_before}->{n_after}")
                med_l = int(statistics.median(liste_n))
                med_g = int(statistics.median(galerie_n))
                rows.append((s, med_l, med_g, " | ".join(detail)))

        print(f"{'source':22} {'med_liste':>9} {'med_galerie':>11}   détail (liste->galerie)")
        print("-" * 90)
        for s, ml, mg, det in rows:
            print(f"{s:22} {str(ml):>9} {str(mg):>11}   {det}")

    asyncio.run(_test())

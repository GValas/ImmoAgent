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
# Coupe-circuit par domaine : après N échecs 429/503 sur un même domaine, on
# cesse de le solliciter pour le reste de la passe (évite le spam de logs et la
# surcharge qui aggrave le rate-limit). reset_breaker() à appeler au début de
# chaque passe d'enrichissement (hunter le fait).
# --------------------------------------------------------------------------- #
_DOMAIN_FAILS: dict[str, int] = {}
_BREAKER_LIMIT = 4


def reset_breaker() -> None:
    _DOMAIN_FAILS.clear()


def _is_rate_limit(e: Exception) -> bool:
    return isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (429, 503)


def _note_fail(dom: str) -> bool:
    """Incrémente le compteur d'échecs du domaine ; True si on vient d'atteindre la
    limite (à logger une seule fois)."""
    n = _DOMAIN_FAILS.get(dom, 0) + 1
    _DOMAIN_FAILS[dom] = n
    return n == _BREAKER_LIMIT


# --------------------------------------------------------------------------- #
# Filtres communs
# --------------------------------------------------------------------------- #

# Fragments d'URL qui ne sont jamais des photos d'annonce.
_BLACKLIST_FRAGMENTS = (
    "logo", "logos", "/logo", "picto", "pictos", "avatar", "icon", "/icons",
    "sprite", "placeholder", "novisu", "no-visu", "no_photo", "nophoto",
    "default", "blank", "1px", "spacer", "transparent", "loader", "loading",
    "/static/", "/_static_/", "/assets/img", "/assets/imgs", "diagrammeenergie",
    "/dpe.", "/dpe/", "diagnostic", "watermark", "facebook", "twitter", "instagram",
    "youtube", "linkedin", "flag-", "/flags/", "profile-picture", "agence-logo",
    "mandataires", "bandeau", "header", "footer", "carte", "/map", "google",
    "/share/", "media-logo", "/logo-",
    # Chrome de site / images non-annonce qui gonflaient le compte (cf. era) :
    "estimation", "homepage", "home-page", "/agency/", "agency/photos",
    "/agence/", "/equipe", "/team-", "/contact", "/banniere",
    "/little/",   # vignette era (/medias/annonces/little/…) ; NB: ne PAS bannir
                  # "mini" générique → green-acres sert ses photos sous /miniPhotos/
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


# --------------------------------------------------------------------------- #
# Extraction DPE (best-effort) — réutilise le HTML/JSON déjà récupéré, AUCUNE
# requête supplémentaire. Renseigne bien['dpe'] = "A".."G" si absent.
#
# Pièges :
#   - Ne JAMAIS prendre le GES (gaz à effet de serre) à la place du DPE.
#     green-acres affiche deux diagrammes `class="letter X active"` : le 1er est
#     le DPE (énergie), le 2nd est le GES (précédé de `ges-line` / `ges`).
#   - Ignorer les DPE "vierge"/"NS"/"non soumis"/"en cours".
#   - Une lettre seule (A-G) ne suffit pas à confirmer un DPE → on exige toujours
#     un contexte (mot-clé énergie, classe, etiquette, dpe…) dans les patterns
#     texte, pour ne pas capturer une lettre au hasard.
# --------------------------------------------------------------------------- #

# Marqueurs indiquant qu'on n'a PAS de classe DPE exploitable.
_DPE_VOID_RE = re.compile(
    r"vierge|non\s*soumis|non\s*concern|en\s*cours|non\s*communiqu|\bNS\b|\bND\b|"
    r"non\s*renseign|non\s*disponible|inconnu|absen",
    re.IGNORECASE,
)

# JSON-LD / attributs data / API : "energyClass":"D", "classeEnergie":"D"...
# (on EXCLUT volontairement les variantes GES : *GreenhouseGas*, *ges*, *emission*).
_DPE_JSON_RE = re.compile(
    r'"(?:energy[_-]?class|energyclass|energyrating|energy[_-]?rating|dpe(?:[_-]?letter)?'
    r'|classe[_-]?energ\w*|classe[_-]?dpe|etiquette[_-]?energ\w*|consommation[_-]?classe'
    r'|diagnostic[_-]?energ\w*|epc[_-]?rating|epc)"\s*:\s*"?\s*([A-G])\b',
    re.IGNORECASE,
)

# green-acres : <div class="letter F active"> (1er = DPE, 2nd = GES).
_DPE_LETTER_ACTIVE_RE = re.compile(
    r'class="letter\s+([A-G])\s+active"', re.IGNORECASE,
)

# Texte SSR : "DPE : F", "Classe énergie : F", "étiquette énergie F"...
_DPE_TEXT_RES = (
    re.compile(r'\bDPE\b[^A-Za-z0-9]{0,12}([A-G])\b'),
    re.compile(r'classe\s*(?:é|e)nerg\w*[^A-Za-z0-9]{0,12}([A-G])\b', re.IGNORECASE),
    re.compile(r'(?:é|e)tiquette\s*(?:é|e)nerg\w*[^A-Za-z0-9]{0,12}([A-G])\b', re.IGNORECASE),
    re.compile(r'performance\s*(?:é|e)nerg\w*[^A-Za-z0-9]{0,12}([A-G])\b', re.IGNORECASE),
)


def _ctx_is_void(txt: str, pos: int, span: int = 60) -> bool:
    """True si le voisinage (±span) du match contient un marqueur 'pas de DPE'."""
    return bool(_DPE_VOID_RE.search(txt[max(0, pos - span):pos + span]))


def extract_dpe(txt: str) -> str | None:
    """Extrait la classe DPE (A-G) depuis le HTML/JSON détail. None si introuvable.

    Best-effort : ne lève jamais. Cascade :
      1) green-acres `letter X active` (1er = DPE ; on saute le bloc GES),
      2) JSON-LD / data / API (energyClass, classeEnergie, consommationClasse…),
      3) texte SSR (DPE : X, classe énergie X, étiquette énergie X…).
    """
    if not txt:
        return None
    try:
        # 1) green-acres : prendre le 1er `letter X active` qui n'est PAS dans le
        # diagramme GES (le bloc GES est précédé de "ges-line"/"ges").
        for m in _DPE_LETTER_ACTIVE_RE.finditer(txt):
            before = txt[max(0, m.start() - 220):m.start()].lower()
            # Si un marqueur GES apparaît APRÈS le dernier marqueur DPE dans le
            # contexte amont, ce diagramme est le GES → on le saute.
            ges_pos = before.rfind("ges")
            dpe_pos = max(before.rfind("dpe"), before.rfind("energ"), before.rfind("énerg"))
            if ges_pos != -1 and ges_pos > dpe_pos:
                continue
            letter = m.group(1).upper()
            if "A" <= letter <= "G":
                return letter

        # 2) JSON / data attributes (exclut déjà les variantes GES par construction).
        for m in _DPE_JSON_RE.finditer(txt):
            if _ctx_is_void(txt, m.start()):
                continue
            # Garde-fou : si "ges"/"greenhouse"/"emission" est collé juste avant la
            # clé capturée, on ignore (faux positif GES).
            key_ctx = txt[max(0, m.start() - 25):m.start()].lower()
            if "ges" in key_ctx or "greenhouse" in key_ctx or "emission" in key_ctx:
                continue
            return m.group(1).upper()

        # 3) Texte SSR.
        for rx in _DPE_TEXT_RES:
            for m in rx.finditer(txt):
                if _ctx_is_void(txt, m.start()):
                    continue
                # Évite de prendre une mention GES ("émission GES : D").
                key_ctx = txt[max(0, m.start() - 12):m.start()].lower()
                if "ges" in key_ctx:
                    continue
                return m.group(1).upper()
    except Exception:
        return None
    return None


def _maybe_set_dpe(bien: dict, txt: str) -> None:
    """Renseigne bien['dpe'] depuis le HTML/JSON si absent. Ne lève jamais."""
    try:
        if bien.get("dpe"):
            return
        letter = extract_dpe(txt)
        if letter:
            bien["dpe"] = letter
    except Exception:
        pass


def _dpe_from_notaires(data: dict) -> str | None:
    """DPE depuis l'API notaires : bien.maison.consommationClasse (≠ emissionGesClasse)."""
    try:
        bien = (data or {}).get("bien") or {}
        for sub in ("maison", "appartement", "terrain", "immeuble"):
            node = bien.get(sub)
            if isinstance(node, dict):
                cls = node.get("consommationClasse")
                if isinstance(cls, str) and len(cls) == 1 and "A" <= cls.upper() <= "G":
                    return cls.upper()
        # repli : champ à la racine du bien
        cls = bien.get("consommationClasse")
        if isinstance(cls, str) and len(cls) == 1 and "A" <= cls.upper() <= "G":
            return cls.upper()
    except Exception:
        pass
    return None


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
    # DPE depuis l'API (réutilise le même fetch) — bien.maison.consommationClasse.
    if not bien.get("dpe"):
        cls = _dpe_from_notaires(data)
        if cls:
            bien["dpe"] = cls
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


_IAD_DETAIL_API = "https://www.iadfrance.fr/api/properties"


async def _g_iad(bien: dict, session: httpx.AsyncClient) -> list[str]:
    """iad — page détail en JS, mais l'API DÉTAIL renvoie le DPE en JSON :
    GET /api/properties/{ref}?locale=fr → epcGes.epc.class (≠ epcGes.ges).
    Nécessite la ref interne `id_annonce` (stockée par iad.py depuis l'API liste).
    Les photos viennent déjà de l'API liste (iad.py) ; ici on récupère surtout le DPE.
    """
    existing = bien.get("photos") or []
    ref = bien.get("id_annonce")
    if not ref:
        return existing
    try:
        data = await _get_json(session, f"{_IAD_DETAIL_API}/{ref}?locale=fr")
    except Exception:
        return existing
    try:
        if not bien.get("dpe"):
            cls = ((data.get("epcGes") or {}).get("epc") or {}).get("class")
            if isinstance(cls, str) and len(cls) == 1 and "A" <= cls.upper() <= "G":
                bien["dpe"] = cls.upper()
    except Exception:
        pass
    return existing


async def _g_century21(bien: dict, session: httpx.AsyncClient) -> list[str]:
    """century21 — galerie en SSR (relatif /imagesBien/...).

    Chaque photo existe en plusieurs tailles : .../c21_..._{IDX}_{GUID}.jpg
    où IDX est l'index de taille (8 = grand, 1 = petit). On dédup par GUID et
    on garde la taille 8.
    """
    txt = await _get_text(session, bien["url"])
    _maybe_set_dpe(bien, txt)
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
    _maybe_set_dpe(bien, txt)
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
    _maybe_set_dpe(bien, txt)
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
    _maybe_set_dpe(bien, txt)
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
    _maybe_set_dpe(bien, txt)
    raw = re.findall(
        r"https://images\.drhouse-immo\.com/minisite/detail/[A-Za-z0-9/.\-]+?\.(?:webp|jpe?g|png)",
        txt,
    )
    return _dedup_keep_order([_clean(u) for u in raw])


async def _g_fnaim(bien: dict, session: httpx.AsyncClient) -> list[str]:
    """fnaim — photos sur imagesv2.fnaim.fr/images1/img/... (logos/ filtrés)."""
    txt = await _get_text(session, bien["url"])
    _maybe_set_dpe(bien, txt)
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
    _maybe_set_dpe(bien, txt)
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

    dom = urlparse(url).netloc
    if _DOMAIN_FAILS.get(dom, 0) >= _BREAKER_LIMIT:
        return existing   # domaine abandonné pour cette passe (rate-limité) — silencieux

    source = (bien.get("source") or "").strip()
    fetcher = _DISPATCH.get(source)

    # 1) Fetcher dédié
    if fetcher is not None:
        try:
            found = await fetcher(bien, session)
            result = _better_of(existing, found, bien)
            if len(result) > len(existing):
                _DOMAIN_FAILS.pop(dom, None)   # succès → reset
                return result
            # Le fetcher dédié n'a rien donné de mieux → on tente le générique,
            # sauf pour les sources JS-only (générique = chrome/placeholder).
        except Exception as e:
            if _is_rate_limit(e):
                if _note_fail(dom):
                    print(f"[Gallery] {dom} rate-limité (429) — abandonné pour ce cycle")
                return existing   # ne PAS tenter le générique (re-429 inutile)
            print(f"[Gallery] {source} fetcher KO ({url[:60]}): {e}")

    if source in _JS_ONLY:
        return existing

    # 2) Fallback générique (SSR / JSON-LD / og:image / <img>)
    try:
        txt = await _get_text(session, url)
        base = url
        _maybe_set_dpe(bien, txt)
        found = _extract_generic(txt, base)
        return _better_of(existing, found, bien)
    except Exception as e:
        if _is_rate_limit(e):
            if _note_fail(dom):
                print(f"[Gallery] {dom} rate-limité (429) — abandonné pour ce cycle")
            return existing
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
        "laforet", "arthurimmo", "webimmo123", "clefrance", "megagence",
        "optimhome", "citya", "squarehabitat", "french_property",
    ]
    PER_SOURCE = 6

    async def _test():
        files = sorted(glob.glob(os.path.join(RAW, "biens_raw_*.json")))
        if not files:
            print("Aucun fichier biens_raw_*.json dans data/raw")
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
        dpe_rows = []
        async with httpx.AsyncClient(headers=_HEADERS, timeout=30) as session:
            for s in TARGET:
                biens = buckets.get(s, [])
                if not biens:
                    rows.append((s, "—", "—", "pas d'échantillon dans le dernier run"))
                    dpe_rows.append((s, "—", "—", 0, "pas d'échantillon"))
                    continue
                liste_n, galerie_n = [], []
                detail = []
                dpe_before = dpe_after = 0
                letters = []
                for b in biens:
                    n_before = len(b.get("photos") or [])
                    had_dpe = bool(b.get("dpe"))
                    if had_dpe:
                        dpe_before += 1
                    b.pop("dpe", None)  # on simule l'absence pour mesurer le gain réel
                    photos = await fetch_gallery(b, session)
                    n_after = len(photos)
                    if b.get("dpe"):
                        dpe_after += 1
                        letters.append(b["dpe"])
                    liste_n.append(n_before)
                    galerie_n.append(n_after)
                    detail.append(f"{n_before}->{n_after}")
                med_l = int(statistics.median(liste_n))
                med_g = int(statistics.median(galerie_n))
                rows.append((s, med_l, med_g, " | ".join(detail)))
                dpe_rows.append((s, dpe_before, dpe_after, len(biens),
                                 "".join(sorted(letters)) or "—"))

        print(f"{'source':22} {'med_liste':>9} {'med_galerie':>11}   détail (liste->galerie)")
        print("-" * 90)
        for s, ml, mg, det in rows:
            print(f"{s:22} {str(ml):>9} {str(mg):>11}   {det}")

        print()
        print("COUVERTURE DPE (orig_avait -> capté / échantillon, ignorant le DPE liste préexistant)")
        print(f"{'source':22} {'avant':>5} {'après':>5} {'/n':>4}   lettres")
        print("-" * 70)
        for s, db, da, n, lts in dpe_rows:
            print(f"{s:22} {str(db):>5} {str(da):>5} {str(n):>4}   {lts}")

    asyncio.run(_test())

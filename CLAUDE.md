# CLAUDE.md — Guide pour Claude Code

Ce fichier décrit le projet immo-agent pour que Claude Code comprenne
l'architecture, les conventions, et comment intervenir efficacement.

---

## Vue d'ensemble

**immo-agent** est un système multi-workers de recherche immobilière automatisée.
Il scrape des sites d'annonces français, filtre les biens sur des critères
structurés (prix, surface, terrain, pièces, DPE), les enrichit (DVF, géoloc, match
qualitatif NLP) et produit un fichier Excel de suivi trié par pertinence qualitative.

> **Filtre visuel CLIP retiré (2026-06-09)** — le worker `vision.py` et la
> dépendance CLIP/torch ont été supprimés pour accélérer le batch. Le filtrage
> se fait désormais sur les critères structurés et la correspondance texte. La
> partie visuelle sera revue ultérieurement (l'historique git conserve
> `workers/vision.py`, la logique d'éléments et l'ancien `config/elements.yaml`).

Le pipeline peut tourner en continu via `scheduler.py`.
**Aucun appel API Anthropic dans le code** — Claude Code (Pro) est le seul point d'intelligence.

---

## Architecture

```
orchestrator.py       Point d'entrée unique (pipeline à la demande)
scheduler.py          Pipeline continu (boucle infinie, fréquences configurables)
config_loader.py      Parse criteria.md → CriteresRecherche (source unique de vérité)
models.py             Dataclasses : Bien, CriteresRecherche

workers/
  discovery.py        Worker 1 — charge sources.yaml, filtre par département
  builder.py          Worker 2 — vérifie scrapers disponibles, liste les manquants
  hunter.py           Worker 3 — scrape parallèle, déduplique, filtre structurel,
                      enrichit page détail (photos/DPE/description), filtre mots-clés,
                      annote gare/bus/géoloc
  analyst.py          Worker 4 — enrichit (DVF, alertes), résumé local, export Excel
  qualitative.py      Util analyst — match NLP description_qualitative ↔ annonce (embeddings, GPU si dispo)

scrapers/             25 scrapers actifs + 4 inactifs (voir liste ci-dessous)
                      Interface obligatoire : async def search(criteres: dict) -> list[dict]

config/
  criteria.md         SEUL fichier édité par l'utilisateur
  sources.yaml        Éditable manuellement ou via Claude Code — contient aussi la blacklist

data/
  raw/                JSON bruts par run (biens_raw_YYYYMMDD_HHMM.json)
  output/             Excel final (resultats_*.xlsx, suivi_actif.xlsx)
  biens_vus.json      Hashes inter-runs pour dédup du scheduler
  scheduler_state.json  Timestamps dernières exécutions
```

---

## Scrapers actifs (41 + DVF)

> Source de vérité : `config/sources.yaml` (le tableau ci-dessous peut être en retard).
> Ajouts 2026-05-30 : cabinet_le_nail, terresetdemeuresdefrance, architecturedecollection,
> french_property, emile_garcin, groupe_mercure, drhouse, exp_france, webimmo123,
> meilleursbiens, imkiz, liberkeys, cimm, le_tuc (filtre département vérifié, 0 fuite).

### API REST (httpx, pas de Playwright)
| Fichier | Site | Notes |
|---------|------|-------|
| `bienici.py` | Bien'ici | API REST avec zone IDs par département |
| `immobilier_notaires.py` | Immobilier.notaires.fr | API officielle notaires, ~24/page |
| `iad.py` | IAD France | API REST, slugs département |
| `remax.py` | RE/MAX France | API POST, post-filtre par zipCode[:2] |
| `era.py` | ERA Immobilier | API v2, filtre code_postal prefix, 10/page |

### httpx pur — SSR (pas de Playwright)
| Fichier | Site | Notes |
|---------|------|-------|
| `lesiteimmo.py` | LeSiteImmo | JSON-LD CollectionPage, 25/page, slugs sarthe-72 |
| `foncia.py` | Foncia Transaction | Angular SSR, div.foncia-card, ~90/dept |
| `seloger.py` | SeLoger | URL legacy list.htm?places=[{cp:XXXXX}], ~18/dept |
| `properstar.py` | Properstar | Agrégateur international, article.item-adaptive, ~20/dept |
| `laforet.py` | Laforêt Immobilier | SSR Symfony, data-gtm-item-*-param, 9 depts (skip 72/36) |
| `arthurimmo.py` | Arthur Immo | Laravel+Livewire SSR, div[wire:id] property.card |
| `entreparticuliers.py` | EntreparticulierS | Hydra/JSON-LD dans \<script\>, P2P, ~23/dept |
| `immonot.py` | Immonot (notaires) | SSR pages régionales, /immobilier-notaire-{region}, ~10/dept |
| `paruvendu.py` | Paru Vendu | SSR HTML, div.blocAnnonce, /vente/maison/{slug}/?p=N |
| `greenacres.py` | Green-Acres | SSR React, div.announce-card, URL en base64 (data-o), propriétés de prestige |

### Playwright + HTML
| Fichier | Site | Notes |
|---------|------|-------|
| `orpi.py` | ORPI | article.c-estate-thumb, filtre via locationIds[] |
| `nestenn.py` | Nestenn | div.bien_item, slugs département |
| `safti.py` | SAFTI | article cards, bonne couverture nationale |
| `ladresse.py` | L'Adresse | a.bien avec data-id |
| `figaro_immo.py` | Figaro Immobilier | article.classified-card, 40/page, post-filtre Python |
| `proprietes_privees.py` | Propriétés Privées | .trade-item-container, ref dans .trade-reference |
| `optimhome.py` | Optimhome | .card.property-card, lien dans data-href |
| `megagence.py` | megAgence | li[class*='list-prop-li'], /acheter/maison/{slug}/ |
| `citya.py` | Citya Immobilier | div.property-card[data-itemid,data-price], 14/page |
| `squarehabitat.py` | Square Habitat | /annonces/achat/.../immobilier/{region}/{dept-slug}, 9-11/dept |

### Inactifs (code conservé, actif: false dans sources.yaml)
- `pap.py` — Cloudflare. À réactiver avec proxy/cookie si contournement trouvé.
- `logic_immo.py` — Cloudflare.
- `annonces_notaires.py` — Immonot 404, remplacé par `immonot.py`.
- `stephaneplaza.py` — Pas de portail national centralisé.

---

## Fichier de configuration unique : criteria.md

**Tout** ce que l'utilisateur configure est dans `config/criteria.md`.
`config_loader.py` le parse et utilise des valeurs par défaut intégrées (dictionnaire
`DEFAULTS`) si une clé est absente — pas de fichier YAML externe.

Organisé en familles de critères. Chaque bloc de code ``` est un **fragment YAML**
(clé: valeur, listes et chaînes multi-lignes, commentaires `#`) ; les dictionnaires
de tous les blocs sont fusionnés. Les départements sont la clé `departements: [..]`
(repli sur les nombres nus du 1ᵉʳ bloc si absente).
- **Phase 1 — Scraping** (champs structurés, filtre dur **au requêtage**) :
  `## Critères du bien` (departements, types, surface, pièces, terrain, prix,
  dpe_exclus, photos_min)
- **Filtre mots-clés** (filtre dur sur le TEXTE, **après** le 1er filtrage) :
  `## Filtre mots-clés` → `mots_obligatoires` (tous exigés, logique ET) et
  `mots_interdits` (un seul présent ⇒ exclu). Appliqué sur titre + description
  **complète** (récupérée en page détail), insensible casse/accents, sous-chaîne.
- **Phase 2 — Analyse** (interprétation) :
  - `## Description qualitative` (texte libre matché à l'annonce via NLP — tri)
  - `## Analyse GÉO` (automatique, non configurable : annotation gare + liens satellite/ortho)
  - `## Critères qualitatifs` (texte libre, non parsé)
- **Pipeline** — `## Scheduler` (intervalles, max_biens_suivi)

---

## Rôle de Claude Code

Claude Code intervient dans 3 situations :

**1. Générer un scraper**
```
"Génère scrapers/nouveausite.py — scrape_simple httpx, url: nouveausite.fr,
 interface async def search(criteres) -> list[dict]"
```
Avant de créer : vérifier la **blacklist** dans `sources.yaml` (section `blacklist:`).
Un site en blacklist ne doit pas être retenté sans changement de stratégie (proxy, cookie réel).

**2. Déboguer un scraper cassé**
```
"foncia.py retourne 0 résultats, lance-le et débogue"
"era.py lève une erreur 403, contourne-la"
```

**3. Analyser les résultats**
```
"Analyse data/output/suivi_actif.xlsx et résume les meilleures opportunités"
"Compare les prix/m² du dernier run avec les références DVF"
```

---

## Conventions de code

- Tout le code est **async/await** (asyncio) — ne pas introduire de code synchrone bloquant
- Les scrapers exposent tous `async def search(criteres: dict) -> list[dict]`
- Le modèle `Bien` (models.py) est la référence pour les clés de dict retournées
- Les workers communiquent via `list[dict]` (pas d'instances Bien) pour flexibilité
- Logging via `print(f"[NomWorker] message")` — pas de logger configuré
- Tester un scraper individuellement : `python scrapers/xxx.py`
- Tester les workers individuellement : `python workers/xxx.py`
- **Pas de fichiers probe_*.py / debug_*.py / test_*.py** à la racine — tout doit finir soit dans un scraper, soit dans la blacklist sources.yaml

---

## Points d'attention

### Match qualitatif NLP (ajouté le 2026-06-09)
- `workers/qualitative.py` compare la `description_qualitative` (criteria.md, texte
  libre FR) au titre + **description complète** de chaque annonce (enrichie en page
  détail par `gallery.py`) par **embeddings de phrases** (`sentence-transformers`,
  modèle `paraphrase-multilingual-MiniLM-L12-v2`, ~120 Mo téléchargé au 1er run).
  **Réintroduit `torch`** (retiré pour CLIP) — assumé.
- **GPU** : le modèle est chargé sur **CUDA si disponible** (device explicite via
  `_pick_device()`, log « … sur CUDA » + nom du GPU), repli **CPU** sinon. Vérifié
  sur RTX 4070 Ti (torch `cu126`). `python workers/qualitative.py` affiche le device.
- **NON éliminatoire** : annote `match_qualitatif` (0–100) et `match_extrait`
  (phrase la plus proche) ; l'analyst **trie** les résultats par `match_qualitatif`
  décroissant + colonnes Excel « Match qual. » / « Extrait qual. ». Désactivé si
  `description_qualitative` est vide.
- **Dégradation gracieuse** : si `sentence-transformers`/modèle indisponible, no-op
  avec avertissement — le pipeline continue sans cette annotation (rien n'est éliminé).
- Les similarités sémantiques se tassent vers 30–60 ; c'est l'**ordre relatif** qui
  compte.

### Filtre mots-clés obligatoires / interdits (ajouté le 2026-06-09)
- Deux listes dans `criteria.md` (`## Filtre mots-clés`) : `mots_obligatoires`
  (logique **ET** — tous exigés) et `mots_interdits` (un seul présent ⇒ exclu).
- **Filtre dur éliminatoire**, mais distinct des critères structurés : il s'applique
  sur le **TEXTE** (titre + description **complète**), donc **après** le 1er filtrage,
  dans `hunter.filter_mots_cles` appelé une fois la page détail enrichie (la vue liste
  ne donne souvent qu'un titre/extrait). Insensible casse/accents, sous-chaîne.
- Listes vides ⇒ filtre désactivé. `filter_biens` ne fait plus que le structurel.

### Enrichissement page détail (`scrapers/gallery.py`)
- Sur les **seuls survivants** du 1er filtrage, `fetch_gallery` visite la page/API
  détail et enrichit, **sans requête supplémentaire** au-delà de ce fetch :
  **photos** (galerie complète), **DPE** (`extract_dpe`, ignore le GES) et désormais
  la **description complète** (`_maybe_set_description` : JSON-LD `description` +
  `og:description` ; `_maybe_set_description_from_json` pour les API type notaires).
  N'écrase la description liste que si la nouvelle est **strictement plus longue**.
- Cette description enrichie alimente le **filtre mots-clés** et le **match
  qualitatif NLP**, qui opèrent ainsi sur l'annonce entière.
- Ne lève jamais (best-effort) ; coupe-circuit 429/503 par domaine.

### Annotation arrêt de bus (`scrapers/bus.py`)
- Annotation **informative, non éliminatoire** (comme la gare) : remplit
  `bus_proche` / `bus_nom` / `bus_distance_km` (colonne Excel « Bus »).
- Lancée par `hunter.py` sur les survivants, après le re-filtre/enrichissement et
  avant la géoloc (`BUS_RAYON_KM = 2.0`). Source : Overpass OSM (coupe-circuit si
  injoignable). Toujours active, plus de toggle dans `criteria.md`.

### Filtre visuel (CLIP) — RETIRÉ le 2026-06-09
- Le worker `workers/vision.py`, la dépendance CLIP/torch et la colonne Excel
  « Éléments détectés » ont été supprimés pour accélérer le batch.
- Le filtrage dur repose désormais uniquement sur les **critères structurés**
  (prix, surface, terrain, pièces, DPE). L'analyse du texte se fait via le **match
  qualitatif NLP** (tri, non éliminatoire — voir ci-dessus).
- Pour réintroduire un filtre visuel plus tard : voir `workers/vision.py` et
  l'ancien `config/elements.yaml` dans l'historique git (commit antérieur au 2026-06-09).

### Scrapers
- Interface obligatoire : `async def search(criteres: dict) -> list[dict]`
- Pour forcer la régénération : supprimer le fichier `.py` et demander à Claude Code
- `builder.py` liste les scrapers manquants au démarrage — s'en servir comme todo list
- **laforet.py** skippe les depts 72 et 36 (pas de page dédiée sur laforet.com) — comportement normal
- **greenacres.py** : URL obfusquée en base64 dans `data-o` ; contenu SSR React (pas de JS nécessaire)

### Déduplication
- Intra-run : hash(prix + surface + ville) dans `hunter.py`
- Inter-runs : `data/biens_vus.json` dans `scheduler.py`

### DVF
- `analyst.py` utilise des prix de référence hardcodés par département (2024)
- Pour des données temps réel, remplacer `_prix_m2_reference()` par un appel API

### Géolocalisation (`scrapers/geolocate.py`)
- Module utilitaire (pas une source) : `search()` renvoie toujours `[]`, comme `gares.py`
- Pré-localise un bien à partir de la position de l'annonce :
  - **Bien'ici** expose `blurInfo` (centre + rayon de floutage ~125 m) → extrait dans
    `latitude`/`longitude`/`blur_radius_m` par `bienici.py`
  - **Sources sans coords dans la liste** : `coords_from_detail()` récupère les
    coordonnées sur la **page/API détail** de l'annonce, au moment de la géoloc et
    sur les seuls biens survivants (quelques requêtes httpx). Stratégie :
    1. **API notaires** (`/v1/annonces/{id}` → `bien.maison.coordonneesExactesW84`)
       → coordonnées **exactes**.
    2. **Parsing HTML générique** (toutes les autres sources), en cascade :
       paires étiquetées (`"lat":x,"lng":y`, `data-latitude/longitude`,
       `L.marker([lat,lon])`, `@lat,lon`) → valeurs lat/lon étiquetées **séparées**
       croisées (citya, megagence) → paires de décimaux adjacents en testant **les
       deux ordres** (lesiteimmo encode lon,lat).
    Chaque candidat est **validé contre le centre commune** (≤ 10 km), ce qui lève
    l'ambiguïté lat/lon et rejette les centroïdes région/pays et coords d'agence.
    Couvre ~12 sources (foncia, seloger, era, immonot, citya, ladresse, laforet,
    lesiteimmo, megagence, proprietes_privees, entreparticuliers, squarehabitat,
    arthurimmo, greenacres…). **Non couvertes** : iad & paruvendu (coords seulement
    en JS, absentes du SSR) et optimhome (403 sous httpx) → lien satellite commune
    seulement ; nécessiteraient Playwright.
  - La **déduplication** (`hunter.py`) fusionne les coords d'un doublon Bien'ici écarté
    vers le bien conservé (Bien'ici aggrège IAD/SAFTI/ERA… → coords sinon perdues).
- Ajoute dans l'Excel les liens `Satellite` (Google) et `Ortho+cadastre`
  (Geoportail, idéal pour la vérification visuelle manuelle), et un flag `geo_precis`.
- Lancé par `hunter.py` en dernier (après les annotations gare et bus) — toujours
  actif, plus de toggle
- **CGU** : on ne scrape pas les tuiles Google (liens pour le clic humain seulement).
- > Le matching cadastral « parcelle probable » (apicarto IGN) a été **retiré le
  > 2026-06-09** ; ne restent que les liens. Voir l'historique git pour le rétablir.

### Anti-bot & blacklist
- **Cloudflare** (LeBonCoin, PAP, Logic-Immo, OuestFrance-Immo, MeilleursAgents) — infranchissable sans proxy rotatif ou cookie de session réel
- **CSR pur** (Keymex, FAI, LKI…) — page vide sous httpx, Playwright requis mais souvent inutile aussi
- **Filtre dept cassé** (GuyHoquet, Swixim, Efficity, ImmoRegion…) — retournent des annonces nationales
- En cas d'échec répété d'un scraper actif : mettre `actif: false` dans sources.yaml + ajouter en blacklist
- Alternative pour sites bloqués : Apify actor ou ScraperAPI

---

## Commandes utiles

```bash
# Installation
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Pas de clé API requise

# Pipeline à la demande
python orchestrator.py                        # pipeline complet
python orchestrator.py --skip-discovery       # sans Discovery
python orchestrator.py --skip-build           # sans Builder
python orchestrator.py --only-analyse         # ré-enrichir le dernier raw

# Pipeline continu
python scheduler.py                           # boucle infinie
python scheduler.py --once                    # un seul cycle (debug)
nohup python scheduler.py > logs/scheduler.log 2>&1 &

# Tester un scraper individuellement
python scrapers/bienici.py
python scrapers/greenacres.py
# etc.

# Workers individuels (debug)
python workers/discovery.py
python workers/builder.py
python workers/hunter.py
python workers/analyst.py
```

---

## Backlog

- [ ] Remplacer les prix DVF hardcodés par un appel API temps réel
- [ ] Ajouter des tests unitaires sur `config_loader.py` (parsing criteria.md)
- [ ] Gérer la rotation de proxies pour les sites anti-bot (PAP, LeBonCoin)
- [ ] Enrichissement géo : score de localisation via API (transports, commerces)
- [ ] Export vers Google Sheets en option
- [ ] Dashboard web simple (FastAPI + Jinja) pour visualiser suivi_actif

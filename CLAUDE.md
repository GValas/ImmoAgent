# CLAUDE.md — Guide pour Claude Code

Ce fichier décrit le projet immo-agent pour que Claude Code comprenne
l'architecture, les conventions, et comment intervenir efficacement.

---

## Vue d'ensemble

**immo-agent** est un système multi-workers de recherche immobilière automatisée.
Il scrape des sites d'annonces français, filtre visuellement les biens via CLIP,
les score selon des critères pondérés, et produit un fichier Excel de suivi.

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
  hunter.py           Worker 3 — scrape en parallèle, déduplique, filtre
  vision.py           Worker 4 — scoring visuel CLIP local (pas d'API externe)
  analyst.py          Worker 5 — scoring pondéré, DVF, résumé local, export Excel

scrapers/             25 scrapers actifs + 4 inactifs (voir liste ci-dessous)
                      Interface obligatoire : async def search(criteres: dict) -> list[dict]

config/
  criteria.md         SEUL fichier édité par l'utilisateur (inclut les exclusions visuelles FR)
  sources.yaml        Éditable manuellement ou via Claude Code — contient aussi la blacklist
  elements.yaml       Éléments visuels à écarter (prompts EN + seuil/mode), synchronisé depuis criteria.md

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

Organisé en deux familles de critères (le parser lit le 1ᵉʳ bloc comme
départements, puis toute ligne `clé: valeur` de n'importe quel bloc) :
- **Zone** — `## Départements` (codes dept, doit rester le 1ᵉʳ bloc)
- **Phase 1 — Scraping** (champs structurés) : `## Critères du bien`
  (types, surface, pièces, terrain, prix, dpe_exclus)
- **Phase 2 — Analyse** (interprétation) :
  - `## Analyse du TEXTE` (mots_cles_negatifs, equipements_requis)
  - `## Analyse VISUELLE` (exclusions visuelles FR → `elements.yaml`)
  - `## Analyse GÉO` (gare/bus, géolocalisation cadastre/ortho)
  - `## Critères qualitatifs` (texte libre, non parsé)
- **Scoring** — `## Pondérations du scoring` (poids_*, auto-normalisé à 100)
- **Pipeline** — `## Scheduler` (intervalles et seuils)

---

## Rôle de Claude Code

Claude Code intervient dans 4 situations :

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

**4. Synchroniser les exclusions visuelles**
```
"synchronise les exclusions visuelles"
```
L'utilisateur décrit en **français** les éléments à écarter dans
`criteria.md` (section `## Analyse VISUELLE — Exclusions`).
Claude Code traduit chaque ligne en prompts **anglais** (CLIP est entraîné en
anglais ; le français inverse la détection — vérifié), choisit les prompts
**négatifs** (confondants proches) et le **seuil**, puis met à jour
`config/elements.yaml` (le fichier réellement lu au runtime). Un élément non
calibré reste en `mode: alerte` (non destructif) jusqu'à validation via
`python workers/vision.py --calibrer <nom>`. Voir aussi `### Détecteur d'éléments`
ci-dessous.

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

### CLIP / Vision
- Modèle CLIP (ViT-B/32) téléchargé automatiquement au premier lancement (~340 MB)
- Cache dans `~/.cache/clip/`
- Embeddings texte (éléments, piscine) mis en cache global de session
- Concurrence limitée à 8 pour les téléchargements de photos (httpx)
- **Plus de scoring de style positif** ni de `style_references/` : le filtre visuel
  est uniquement par exclusion d'éléments (voir ci-dessous)

### Détecteur d'éléments (`config/elements.yaml`)
- Remplace l'ancien ban par image (`config/style_ban/`, supprimé). Filtre par
  **présence d'un élément** dans les photos, pas par ressemblance globale.
- Chaque élément = `positifs`/`negatifs` (prompts EN) + `seuil` + `mode`
  (`exclusion` | `alerte`). CLIP zero-shot contrastif (présent vs absent), MAX sur
  les photos du bien. `description` est en français, documentaire (ignorée du moteur).
- **Prompts en anglais obligatoires** (CLIP est EN ; le FR inverse la détection).
- Précision = qualité des `negatifs` (confondants) + seuil **calibré par élément** :
  `python workers/vision.py --calibrer <nom>`. CLIP ne sépare pas finement des features
  proches (ex. piscine hors-sol vs creusée+bois) → seuils prudents, démarrer en `alerte`.
- `vision.rescore_elements` (appelé par `scheduler.update_suivi`) ré-évalue
  rétroactivement les entrées legacy du suivi quand un élément est ajouté.
- Colonne Excel « Éléments détectés » (suivi + resultats).

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
  - croise avec le **cadastre IGN** (`apicarto.ign.fr`, gratuit) : ne garde que les
    parcelles dont la `contenance` ≈ `surface_terrain` annoncée (±`geoloc_terrain_tol_pct`)
- Ajoute dans l'Excel : `Parcelle probable`, liens `Satellite` (Google) et `Ortho+cadastre`
  (Geoportail, idéal pour la vérification visuelle manuelle)
- Lancé par `hunter.py` après le filtre gare, si `geoloc_actif` (criteria.md)
- **Limites** :
  - Parcelles candidates uniquement si coords précises (Bien'ici `blurInfo`) ; le repli
    centre-commune ne donne qu'un lien satellite.
- **CGU** : on ne scrape pas les tuiles Google (liens pour le clic humain seulement).

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
pip install git+https://github.com/openai/CLIP.git
playwright install chromium

# Pas de clé API requise

# Pipeline à la demande
python orchestrator.py                        # pipeline complet
python orchestrator.py --skip-discovery       # sans Discovery
python orchestrator.py --skip-build           # sans Builder
python orchestrator.py --only-analyse         # re-scorer le dernier raw

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
python workers/vision.py
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

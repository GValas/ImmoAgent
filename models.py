"""
models.py — Modèles de données partagés entre tous les workers
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Bien:
    """Représente un bien immobilier normalisé, quelle que soit la source."""

    # Identification
    source: str
    url: str
    id_annonce: Optional[str] = None

    # Descriptif
    titre: str = ""
    type_bien: str = ""          # maison / appartement / villa / terrain
    description: str = ""

    # Localisation
    departement: str = ""
    ville: str = ""
    code_postal: str = ""
    adresse: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    blur_radius_m: Optional[float] = None   # rayon de floutage de la position (m)

    # Pré-localisation cadastrale (rempli par scrapers/geolocate.py)
    geo_precis: Optional[bool] = None        # coords issues de l'annonce (vs centre commune)
    maps_satellite_url: Optional[str] = None
    geoportail_url: Optional[str] = None     # ortho IGN + parcellaire superposés
    cadastre_url: Optional[str] = None

    # Gare SNCF voyageurs la plus proche (rempli par scrapers/gares.py)
    gare: Optional[bool] = None
    gare_nom: Optional[str] = None
    gare_distance_km: Optional[float] = None

    # Arrêt de bus le plus proche (rempli par scrapers/bus.py — informatif, non éliminatoire)
    bus_proche: Optional[bool] = None
    bus_nom: Optional[str] = None
    bus_distance_km: Optional[float] = None

    # Caractéristiques
    surface: Optional[float] = None       # m²
    surface_terrain: Optional[float] = None
    terrain_estime_texte: Optional[bool] = None  # surface_terrain déduite de la description
    pieces: Optional[int] = None
    chambres: Optional[int] = None
    etage: Optional[int] = None
    dpe: Optional[str] = None             # A/B/C/D/E/F/G
    has_pool: Optional[bool] = None       # piscine signalée par au moins une source

    # Prix
    prix: Optional[float] = None
    prix_m2: Optional[float] = None
    charges: Optional[float] = None

    # Méta
    date_publication: Optional[datetime] = None
    date_scraped: datetime = field(default_factory=datetime.now)
    date_ajout_suivi: Optional[str] = None     # date d'entrée dans suivi_actif (YYYY-MM-DD)
    photos: list[str] = field(default_factory=list)
    agence: Optional[str] = None

    # Liens géo (remplis par scrapers/geolocate.py)
    rome2rio_url: Optional[str] = None
    geo_source: Optional[str] = None

    # Enrichissement Worker Analyst
    match_qualitatif: Optional[float] = None   # similarité NLP description ↔ annonce (0–100)
    match_extrait: Optional[str] = None        # phrase de l'annonce la plus proche
    prix_m2_calcule: Optional[float] = None    # prix / surface
    prix_m2_marche_dep: Optional[float] = None  # prix médian €/m² du département (DVF)
    alerte: list[str] = field(default_factory=list)   # anomalies détectées (prix, DPE…)

    def hash_dedup(self) -> str:
        """Clé de déduplication basée sur prix + surface + ville (cf. core.dedup)."""
        from core.dedup import dedup_hash
        return dedup_hash(self.to_dict())

    def prix_m2_calc(self) -> Optional[float]:
        if self.prix and self.surface and self.surface > 0:
            return round(self.prix / self.surface, 0)
        return None

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["date_publication"] = self.date_publication.isoformat() if self.date_publication else None
        d["date_scraped"] = self.date_scraped.isoformat()
        return d


@dataclass
class CriteresRecherche:
    """Critères parsés depuis criteria.md (seule source ; cf. config_loader.py)."""
    departements: list[str]
    types_bien: list[str]
    surface_min: int
    surface_max: int
    prix_min: int
    prix_max: int
    pieces_min: int
    pieces_max: int
    terrain_min: int
    dpe_exclus: list[str]
    description_qualitative: str = ""   # texte libre matché à l'annonce (NLP)
    photos_min: int = 0          # nb minimal de photos (0 = pas de filtre)
    mots_obligatoires: list[str] = field(default_factory=list)  # tous exigés dans le texte (ET)
    mots_interdits: list[str] = field(default_factory=list)     # exclu si l'un est présent

    # ── Paramètres scheduler (## Scheduler dans criteria.md) ──
    hunter_interval_hours: float = 4      # fréquence Hunter+Analyst (nouvelles annonces)
    discovery_interval_days: float = 7    # fréquence Discovery (re-qualifier les sources)
    builder_interval_days: float = 30     # fréquence Builder (scrapers à jour)
    max_biens_suivi: int = 50             # plafond de biens conservés dans suivi_actif

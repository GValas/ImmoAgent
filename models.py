"""
models.py — Modèles de données partagés entre tous les agents
"""
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime
import hashlib


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
    parcelles_candidates: list = field(default_factory=list)
    parcelle_match: Optional[str] = None     # "Section Numéro — N m²"

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
    pieces: Optional[int] = None
    chambres: Optional[int] = None
    etage: Optional[int] = None
    dpe: Optional[str] = None             # A/B/C/D/E/F/G

    # Prix
    prix: Optional[float] = None
    prix_m2: Optional[float] = None
    charges: Optional[float] = None

    # Méta
    date_publication: Optional[datetime] = None
    date_scraped: datetime = field(default_factory=datetime.now)
    photos: list[str] = field(default_factory=list)
    agence: Optional[str] = None

    # Analyse visuelle (remplie par Agent Vision dans Hunter)
    resume_visuel: Optional[str] = None               # phrase de synthèse
    elements_detectes: list = field(default_factory=list)  # éléments indésirables détectés
    banni: bool = False                               # un élément en mode exclusion détecté
    nb_photos_analysees: int = 0

    # Scoring final (rempli par Agent Analyst)
    score_total: Optional[float] = None
    score_detail: dict = field(default_factory=dict)
    alerte: list[str] = field(default_factory=list)   # anomalies détectées

    def hash_dedup(self) -> str:
        """Clé de déduplication basée sur prix + surface + ville."""
        key = f"{self.prix}-{self.surface}-{self.ville.lower().strip()}"
        return hashlib.md5(key.encode()).hexdigest()

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
    """Critères parsés depuis criteria.md + criteres.yaml"""
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
    mots_cles_negatifs: list[str]
    equipements_requis: list[str]
    poids_scoring: dict
    photos_min: int = 0          # nb minimal de photos (0 = pas de filtre)
    gare_obligatoire: bool = False
    gare_rayon_km: float = 10.0
    bus_actif: bool = True
    bus_rayon_km: float = 2.0
    geoloc_actif: bool = True
    geoloc_terrain_tol_pct: float = 25.0

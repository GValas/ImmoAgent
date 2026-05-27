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

    # Scoring visuel (rempli par Agent Vision dans Hunter)
    score_visuel: Optional[float] = None              # 0–100
    verdict_visuel: Optional[str] = None              # match | partiel | exclu
    resume_visuel: Optional[str] = None               # phrase de synthèse
    points_positifs_visuel: list[str] = field(default_factory=list)
    points_negatifs_visuel: list[str] = field(default_factory=list)
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

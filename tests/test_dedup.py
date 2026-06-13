"""Tests de la clé de déduplication unique (core.dedup)."""
from core.dedup import dedup_hash, dedup_key
from models import Bien


def test_dedup_key_normalise_ville():
    # Casse et espaces de la ville sont normalisés.
    assert dedup_key(100000, 90, "  Le Mans ") == dedup_key(100000, 90, "le mans")


def test_dedup_key_ville_none():
    # Une ville None ne lève pas et équivaut à une ville vide.
    assert dedup_key(100000, 90, None) == dedup_key(100000, 90, "")


def test_dedup_hash_identique_si_meme_bien():
    a = {"prix": 250000, "surface": 120, "ville": "Tours"}
    b = {"prix": 250000, "surface": 120, "ville": "TOURS", "url": "autre"}
    assert dedup_hash(a) == dedup_hash(b)


def test_dedup_hash_differe_si_prix_different():
    a = {"prix": 250000, "surface": 120, "ville": "Tours"}
    b = {"prix": 260000, "surface": 120, "ville": "Tours"}
    assert dedup_hash(a) != dedup_hash(b)


def test_model_bien_hash_coherent_avec_dedup_hash():
    # models.Bien.hash_dedup délègue désormais à core.dedup (plus de divergence).
    bien = Bien(source="x", url="u", prix=300000, surface=100, ville="Angers")
    assert bien.hash_dedup() == dedup_hash({"prix": 300000, "surface": 100, "ville": "Angers"})

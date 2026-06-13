"""Tests des filtres a posteriori (core.filters) — code non-réseau, pur."""
import pytest

from core.filters import (
    apply_posterior_filters,
    extract_terrain_from_text,
    filter_biens,
    filter_mots_cles,
    present_affirmative,
)
from models import CriteresRecherche


def make_criteres(**overrides) -> CriteresRecherche:
    base = dict(
        departements=["72"], types_bien=["maison"],
        surface_min=80, surface_max=600, prix_min=0, prix_max=600000,
        pieces_min=4, pieces_max=20, terrain_min=200, dpe_exclus=["G"],
    )
    base.update(overrides)
    return CriteresRecherche(**base)


# ── filter_biens (structurel) ──

def test_filter_biens_exclut_prix_au_dessus_du_max():
    crit = make_criteres(prix_max=300000)
    biens = [{"prix": 250000}, {"prix": 400000}]
    assert filter_biens(biens, crit) == [{"prix": 250000}]


def test_filter_biens_exclut_sous_prix_min():
    crit = make_criteres(prix_min=100000)
    biens = [{"prix": 50000}, {"prix": 150000}]
    assert filter_biens(biens, crit) == [{"prix": 150000}]


def test_filter_biens_exclut_dpe():
    crit = make_criteres(dpe_exclus=["F", "G"])
    biens = [{"dpe": "C"}, {"dpe": "g"}, {"dpe": "F"}]
    assert filter_biens(biens, crit) == [{"dpe": "C"}]


def test_filter_biens_surface_et_pieces():
    crit = make_criteres(surface_min=80, surface_max=200, pieces_min=4)
    biens = [
        {"surface": 100, "pieces": 5},   # ok
        {"surface": 50, "pieces": 5},    # surface trop petite
        {"surface": 100, "pieces": 2},   # pas assez de pièces
        {"surface": 300, "pieces": 5},   # surface trop grande
    ]
    assert filter_biens(biens, crit) == [{"surface": 100, "pieces": 5}]


def test_filter_biens_terrain_absent_non_exclu():
    # Un bien sans surface_terrain renseignée n'est PAS exclu par terrain_min.
    crit = make_criteres(terrain_min=500)
    biens = [{"prix": 100000}, {"surface_terrain": 100}]
    assert filter_biens(biens, crit) == [{"prix": 100000}]


# ── filter_mots_cles ──

def test_mots_interdits_exclut():
    crit = make_criteres(mots_interdits=["appartement"])
    biens = [{"titre": "Belle maison", "description": ""},
             {"titre": "Appartement T3", "description": ""}]
    assert filter_mots_cles(biens, crit) == [biens[0]]


def test_mots_obligatoires_logique_et():
    crit = make_criteres(mots_obligatoires=["piscine", "garage"])
    biens = [
        {"titre": "Maison avec piscine et garage", "description": ""},
        {"titre": "Maison avec piscine", "description": ""},
    ]
    assert filter_mots_cles(biens, crit) == [biens[0]]


def test_present_affirmative_ecarte_negation():
    assert present_affirmative("belle maison avec piscine", "piscine") is True
    assert present_affirmative("maison sans piscine", "piscine") is False
    assert present_affirmative("emplacement pour piscine", "piscine") is False


# ── extraction terrain ──

@pytest.mark.parametrize("texte,attendu", [
    ("Maison avec terrain de 412 m²", 412.0),
    ("4 292 m² de terrain arboré", 4292.0),
    ("parcelle d'environ 1 500 m2", 1500.0),
    ("Aucune mention de surface extérieure", None),
])
def test_extract_terrain_from_text(texte, attendu):
    assert extract_terrain_from_text(texte) == attendu


# ── apply_posterior_filters (séquence complète) ──

def test_apply_posterior_filters_chaine_complete():
    crit = make_criteres(prix_max=300000, mots_interdits=["bureau"], photos_min=2)
    biens = [
        {"prix": 200000, "titre": "Maison", "photos": ["a", "b"]},   # gardé
        {"prix": 500000, "titre": "Maison", "photos": ["a", "b"]},   # prix
        {"prix": 200000, "titre": "Bureau", "photos": ["a", "b"]},   # mot interdit
        {"prix": 200000, "titre": "Maison", "photos": ["a"]},        # photos_min
    ]
    out = apply_posterior_filters(biens, crit)
    assert out == [biens[0]]


def test_apply_posterior_filters_dept_guard():
    crit = make_criteres(departements=["72"])
    biens = [{"code_postal": "72000", "prix": 100000},
             {"code_postal": "06000", "prix": 100000}]
    out = apply_posterior_filters(biens, crit, dept_guard=True)
    assert out == [biens[0]]

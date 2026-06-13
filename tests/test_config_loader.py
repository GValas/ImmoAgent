"""Tests du parsing de criteria.md (config_loader) — source unique de vérité."""
from config_loader import _coerce_departements, _coerce_mots, load_criteria


def test_load_criteria_charge_le_fichier_reel():
    # Le criteria.md du dépôt doit se parser sans erreur et fournir des champs valides.
    c = load_criteria()
    assert isinstance(c.departements, list)
    assert c.prix_max > 0
    assert c.surface_min >= 0


def test_load_criteria_expose_les_champs_scheduler():
    # Les clés scheduler remontent désormais dans CriteresRecherche (parseur unique).
    c = load_criteria()
    assert c.hunter_interval_hours > 0
    assert c.max_biens_suivi > 0


def test_coerce_departements_formats_varies():
    assert _coerce_departements([72, 6, "2b", 971]) == ["72", "06", "2B", "971"]


def test_coerce_departements_ignore_invalides():
    assert _coerce_departements(["72", "abc", ""]) == ["72"]


def test_coerce_mots_accepte_chaine_ou_liste():
    assert _coerce_mots("piscine") == ["piscine"]
    assert _coerce_mots(["a", " ", "b"]) == ["a", "b"]
    assert _coerce_mots(None) == []

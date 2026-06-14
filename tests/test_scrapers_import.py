"""Smoke-test : tous les scrapers s'importent et exposent `async def search`.

C'est le garde-fou clé pour 340 scrapers fragiles : il attrape une erreur de
syntaxe ou un contrat cassé (search absent / non-coroutine) sans toucher au réseau.
"""
import asyncio
import importlib.util
from pathlib import Path

import pytest

SCRAPERS_DIR = Path(__file__).parent.parent / "scrapers"
# Modules utilitaires de scrapers/ qui ne sont PAS des sources : gallery n'expose
# pas search() ; _base est le socle commun ; _ac3_immo / _geo_resolve(r) /
# _notaires_genapi sont des socles de parsing partagés (préfixe _, pas de search()).
# (bus/gares/geolocate/dvf exposent un search() -> [] et restent dans le smoke-test.)
_UTILS = {
    "gallery", "_base", "_ac3_immo", "_geo_resolve", "_geo_resolver", "_notaires_genapi",
}

SCRAPER_FILES = sorted(
    p for p in SCRAPERS_DIR.glob("*.py")
    if not p.stem.startswith("__") and p.stem not in _UTILS
)


def _import_scraper(path: Path):
    spec = importlib.util.spec_from_file_location(f"_smoke_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", SCRAPER_FILES, ids=lambda p: p.stem)
def test_scraper_importe_et_expose_search(path):
    module = _import_scraper(path)
    assert hasattr(module, "search"), f"{path.stem} : pas de fonction search()"
    assert asyncio.iscoroutinefunction(module.search), \
        f"{path.stem} : search() doit être async"

import teledyne_lecroy as facade
import teledyne_lecroy_core as core


def test_facade_exports_match_core() -> None:
    assert facade.__all__ == core.__all__


def test_facade_symbols_reference_core() -> None:
    for name in facade.__all__:
        assert getattr(facade, name) is getattr(core, name)

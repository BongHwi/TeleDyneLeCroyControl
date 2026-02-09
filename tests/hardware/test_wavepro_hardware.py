from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from teledyne_lecroy import (
    AcquisitionConfig,
    ChannelConfig,
    ChannelTrigger,
    ScopeConnectionError,
    TriggerConfig,
    TriggerState,
    WavePro,
    WaveRunner,
)

pytestmark = [pytest.mark.hardware]


def _make_scope():
    address = os.getenv("LECROY_SCOPE_ADDRESS", "localhost")
    protocol = os.getenv("LECROY_SCOPE_PROTOCOL", "vicp")
    model = os.getenv("LECROY_SCOPE_MODEL", "wavepro").lower()
    if model == "waverunner":
        return WaveRunner(address, protocol=protocol, timeout=10.0)
    return WavePro(address, protocol=protocol, timeout=10.0)


@pytest.fixture(scope="session", autouse=True)
def _artifact_dir() -> Path:
    root = Path("artifacts") / "lecroy"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def _vbs_log_file(_artifact_dir: Path):
    logger = logging.getLogger("teledyne_lecroy.vbs")
    logger.setLevel(logging.DEBUG)
    path = _artifact_dir / "vbs_debug.log"
    handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        yield path
    finally:
        logger.removeHandler(handler)
        handler.close()


def _append_runstate(_artifact_dir: Path, line: str) -> None:
    path = _artifact_dir / "runstate_transition.log"
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def test_hardware_connect_idn(_artifact_dir: Path, _vbs_log_file: Path) -> None:
    scope = _make_scope()
    try:
        scope.connect()
    except ScopeConnectionError as exc:
        _append_runstate(_artifact_dir, f"connect_failed: {exc}")
        raise
    try:
        idn = scope.query("*IDN?")
        assert idn
        _append_runstate(_artifact_dir, f"connected: {idn}")
    finally:
        scope.disconnect()


@pytest.mark.sequence
def test_hardware_sequence_force_capture(_artifact_dir: Path, _vbs_log_file: Path) -> None:
    scope = _make_scope()
    try:
        scope.connect()
    except ScopeConnectionError as exc:
        _append_runstate(_artifact_dir, f"sequence_connect_failed: {exc}")
        raise

    try:
        scope.configure(
            channels={1: ChannelConfig(vdiv=0.2, offset=0.0, enabled=True)},
            acquisition=AcquisitionConfig(tdiv=1e-3, sampling_period=1e-6),
        )
        scope.set_trigger(
            TriggerConfig(
                channels={1: ChannelTrigger(state=TriggerState.HIGH, level=0.0)},
                mode="NORM",
            )
        )
        scope.set_trigger_mode("NORM")
        _append_runstate(_artifact_dir, "set_trigger_mode:NORM")

        scope.arm(force=True)
        _append_runstate(_artifact_dir, "armed_force")

        scope.wait_for_trigger(timeout=5.0, force=True)
        _append_runstate(_artifact_dir, "wait_for_trigger_done")

        data = scope.readout(channels=[1])
        assert 1 in data
        assert len(data[1].raw_data) > 0
        _append_runstate(_artifact_dir, f"readout_points:{len(data[1].raw_data)}")
    finally:
        scope.disconnect()

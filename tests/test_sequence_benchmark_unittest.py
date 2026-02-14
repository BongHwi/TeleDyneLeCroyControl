from __future__ import annotations

from scripts.benchmark_sequence_latency import run_once


class _FakeInstr:
    def __init__(self) -> None:
        self.timeout = 1000
        self.writes: list[str] = []

    def write(self, command: str) -> None:
        self.writes.append(command)

    def read_raw(self) -> bytes:
        return b"#2101234567890"


class _FakeScope:
    def __init__(self) -> None:
        self._scope = _FakeInstr()
        self.writes: list[str] = []

    def apply_settings(self, _settings):
        return None

    def configure(self, **_kwargs):
        return None

    def set_trigger(self, _cfg):
        return None

    def set_trigger_mode(self, _mode: str):
        return None

    def arm(self, force: bool = False):
        return None

    def wait_for_trigger(self, timeout: float, force: bool = False):
        return None

    def query(self, cmd: str) -> str:
        if cmd == "*OPC?":
            return "1"
        return ""

    def write(self, cmd: str) -> None:
        self.writes.append(cmd)


def test_latency_breakdown_fields_present() -> None:
    rec = run_once(
        _FakeScope(),
        channels_on=[1, 2],
        segments=10,
        tdiv=1e-9,
        sampling_period=1e-10,
        wait_timeout=1.0,
        opc_timeout=1.0,
        sync_mode="wait_then_opc",
        sn_mode="all",
        batch_segments=100,
        np_points=1000,
        sp=1,
        display="OFF",
        postproc_profile="minimal",
    )

    assert rec["timeout_flag"] == 0
    assert rec["t_wait"] >= 0.0
    assert rec["t_opc"] >= rec["t_wait"]
    assert "t_firstbyte" in rec
    assert "t_xfer_done" in rec
    assert rec["bytes_received"] > 0

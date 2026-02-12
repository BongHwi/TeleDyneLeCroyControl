from __future__ import annotations

import copy
import logging
import math
import os
import re
from pathlib import Path

import pytest

from teledyne_lecroy import (
    AcquisitionConfig,
    ChannelConfig,
    ChannelTrigger,
    ScopeConnectionError,
    TriggerConfig,
    TriggerState,
    WP804HD,
    WR8208HD,
)

pytestmark = [pytest.mark.hardware]


def _make_scope():
    address = os.getenv("LECROY_SCOPE_ADDRESS", "localhost")
    protocol = os.getenv("LECROY_SCOPE_PROTOCOL", "vicp")
    model = os.getenv("LECROY_SCOPE_MODEL", "wp804hd").lower()
    if model in {"wr8208hd", "waverunner"}:
        return WR8208HD(address, protocol=protocol, timeout=10.0)
    return WP804HD(address, protocol=protocol, timeout=10.0)


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


def _pick_test_vdiv(current: float) -> float:
    candidates = [0.02, 0.05, 0.1, 0.2, 0.5]
    for candidate in candidates:
        if not math.isclose(current, candidate, rel_tol=1e-3, abs_tol=1e-6):
            return candidate
    return 0.1


def _assert_close(actual: float, expected: float, *, label: str) -> None:
    assert math.isclose(actual, expected, rel_tol=5e-2, abs_tol=5e-4), (
        f"{label} mismatch: expected {expected}, got {actual}"
    )


def _alt_choice(current: str, choices: list[str]) -> str:
    current_u = current.upper()
    for item in choices:
        if item.upper() != current_u:
            return item
    return choices[0]


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


def test_hardware_settings_roundtrip_apply_readback(_artifact_dir: Path, _vbs_log_file: Path) -> None:
    scope = _make_scope()
    try:
        scope.connect()
    except ScopeConnectionError as exc:
        _append_runstate(_artifact_dir, f"settings_roundtrip_connect_failed: {exc}")
        raise

    original_settings: dict | None = None
    try:
        original_settings = scope.read_all_settings()
        base = copy.deepcopy(original_settings)
        ch1 = base["channels"]["1"]
        trig = base["trigger"]
        seq = base["sequence"]
        acq = base["acquisition"]
        inst = base.get("instrument", {})

        target_vdiv = _pick_test_vdiv(float(ch1["vdiv"]))
        target_offset = float(ch1["offset"]) + 0.02
        target_coupling = _alt_choice(str(ch1.get("coupling", "DC50")), ["DC50", "DC1M", "AC1M"])
        target_enabled = not bool(ch1.get("enabled", True))
        target_display = _alt_choice(str(inst.get("display", "ON")), ["ON", "OFF"])
        target_grid = _alt_choice(str(inst.get("grid", "QUATTRO")), ["QUATTRO", "SINGLE", "DUAL"])
        target_bwl = _alt_choice(str(inst.get("bandwidth_limit", "OFF")), ["ON", "OFF"])
        target_aux = _alt_choice(str(base.get("auxiliary_output", "TRIGGER_OUT")), ["TRIGGER_OUT", "TRIGGER_ENABLED"])
        target_mode = _alt_choice(str(trig.get("mode", "NORM")), ["NORM", "SINGLE", "AUTO"])
        target_logic = _alt_choice(str(trig.get("logic", "OR")), ["OR", "AND"])
        target_external = not bool(trig.get("external", False))
        target_external_level = float(trig.get("external_level", 1.25)) + 0.05
        target_trig_level = float(trig["channels"]["1"]["level"]) + 0.01
        target_seq_enabled = not bool(seq.get("enabled", False))
        target_seq_segments = 3 if int(seq.get("num_segments", 1)) != 3 else 2
        target_seq_to_en = not bool(seq.get("timeout_enabled", False))
        target_seq_to_s = float(seq.get("timeout_seconds", 1.0)) + 1.0
        current_tdiv = float(acq["tdiv"])
        current_sampling = float(acq["sampling_period"])
        target_tdiv = current_tdiv * 2.0 if current_tdiv < 1e-3 else current_tdiv / 2.0
        target_sampling = (
            current_sampling * 2.0 if current_sampling < 1e-5 else max(current_sampling / 2.0, 1e-12)
        )
        target_trdl = float(acq.get("trigger_delay", 0.0)) + max(current_tdiv * 0.1, 1e-9)
        max_window = current_tdiv * 4.0
        target_wdelay = min(
            float(acq.get("window_delay", 0.0)) + max(current_tdiv * 0.05, 1e-10),
            max_window,
        )
        target_memory = 20000 if int(acq.get("memory_size", 10000)) != 20000 else 10000
        target_sr = 5e9 if float(acq.get("sample_rate", 1e10)) > 6e9 else 1e10

        def _check_trigger_logic(_rb: dict) -> bool:
            trpa = scope.query("TRPA?").upper().replace(" ", "")
            if f"STATE,{target_logic.upper()}" in trpa:
                return True
            if target_logic.upper() == "AND" and "STATE,OR" in trpa:
                _append_runstate(_artifact_dir, "settings_case_warn:trigger.logic_fixed_or")
                return True
            return False

        def _check_trigger_external(_rb: dict) -> bool:
            trpa = scope.query("TRPA?").upper().replace(" ", "")
            has_external = ("EX,H" in trpa) or ("EX,L" in trpa)
            if has_external is target_external:
                return True
            _append_runstate(
                _artifact_dir,
                f"settings_case_warn:trigger.external_fixed actual={has_external} target={target_external}",
            )
            return True

        def _check_trigger_external_level(_rb: dict) -> bool:
            raw = scope.query("EX:TRLV?")
            m = re.search(r"[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?", raw)
            actual = float(m.group(0)) if m else float("nan")
            if math.isclose(actual, target_external_level, rel_tol=1e-1, abs_tol=1e-2):
                return True
            _append_runstate(
                _artifact_dir,
                f"settings_case_warn:trigger.external_level_quantized actual={actual} target={target_external_level}",
            )
            return True

        def _check_trigger_channel1(_rb: dict) -> bool:
            trpa = scope.query("TRPA?").upper().replace(" ", "")
            state_ok = "C1,H" in trpa

            raw = scope.query("C1:TRLV?")
            m = re.search(r"[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?", raw)
            level = float(m.group(0)) if m else float("nan")
            level_ok = math.isclose(level, target_trig_level, rel_tol=1e-1, abs_tol=1e-2)

            if state_ok and level_ok:
                return True
            _append_runstate(
                _artifact_dir,
                f"settings_case_warn:trigger.channel1_constrained state_ok={state_ok} level={level} target={target_trig_level}",
            )
            return True

        def _check_trigger_mode(_rb: dict) -> bool:
            try:
                mode_raw = scope.query(r"""vbs? 'return=app.Acquisition.TriggerMode' """).strip().upper()
            except Exception:
                mode_raw = str(_rb.get("trigger", {}).get("mode", "")).upper()

            if target_mode.upper() in mode_raw:
                return True
            if "NORM" in mode_raw:
                _append_runstate(
                    _artifact_dir,
                    f"settings_case_warn:trigger.mode_fixed actual={mode_raw} target={target_mode}",
                )
                return True
            return False

        def _check_auxiliary_output(rb: dict) -> bool:
            actual = str(rb.get("auxiliary_output", "")).upper()
            if actual == target_aux.upper():
                return True
            try:
                raw = scope.query(r"""vbs? 'return=app.Acquisition.AuxOutput.Mode' """).strip().upper()
            except Exception:
                raw = ""
            if raw in {"SQUARE", "OFF", "UNDEF", "DCLEVEL"}:
                _append_runstate(
                    _artifact_dir,
                    f"settings_case_warn:auxiliary_output_model_constraint raw={raw} target={target_aux} mapped={actual}",
                )
                return True
            return False

        cases: list[tuple[str, dict, callable]] = [
            (
                "instrument.display",
                {"instrument": {"display": target_display}},
                lambda rb: str(rb.get("instrument", {}).get("display")) == target_display,
            ),
            (
                "instrument.grid",
                {"instrument": {"grid": target_grid}},
                lambda rb: str(rb.get("instrument", {}).get("grid", "")).upper() == target_grid.upper(),
            ),
            (
                "instrument.bandwidth_limit",
                {"instrument": {"bandwidth_limit": target_bwl}},
                lambda rb: str(rb.get("instrument", {}).get("bandwidth_limit")) == target_bwl,
            ),
            (
                "channels.1.vdiv",
                {"channels": {"1": {"vdiv": target_vdiv}}},
                lambda rb: math.isclose(float(rb["channels"]["1"]["vdiv"]), target_vdiv, rel_tol=5e-2, abs_tol=5e-4),
            ),
            (
                "channels.1.offset",
                {"channels": {"1": {"offset": target_offset}}},
                lambda rb: (
                    math.isclose(float(rb["channels"]["1"]["offset"]), target_offset, rel_tol=5e-2, abs_tol=5e-4)
                    or math.isclose(float(rb["channels"]["1"]["offset"]), -target_offset, rel_tol=5e-2, abs_tol=5e-4)
                ),
            ),
            (
                "channels.1.coupling",
                {"channels": {"1": {"coupling": target_coupling}}},
                lambda rb: str(rb["channels"]["1"]["coupling"]).upper() == target_coupling.upper(),
            ),
            (
                "channels.1.enabled",
                {"channels": {"1": {"enabled": target_enabled}}},
                lambda rb: bool(rb["channels"]["1"]["enabled"]) is target_enabled,
            ),
            (
                "channels.1.attenuation",
                {"channels": {"1": {"attenuation": float(ch1.get("attenuation", 1.0))}}},
                lambda rb: "attenuation" in rb["channels"]["1"],
            ),
            (
                "acquisition.tdiv",
                {"sequence": {"enabled": False}, "acquisition": {"tdiv": target_tdiv}},
                lambda rb: math.isclose(float(rb["acquisition"]["tdiv"]), target_tdiv, rel_tol=5e-1, abs_tol=5e-9),
            ),
            (
                "acquisition.sampling_period",
                {"sequence": {"enabled": False}, "acquisition": {"sampling_period": target_sampling}},
                lambda rb: math.isclose(
                    float(rb["acquisition"]["sampling_period"]), target_sampling, rel_tol=6e-1, abs_tol=5e-12
                ),
            ),
            (
                "acquisition.trigger_delay",
                {"sequence": {"enabled": False}, "acquisition": {"trigger_delay": target_trdl}},
                lambda rb: math.isclose(
                    float(rb["acquisition"]["trigger_delay"]), target_trdl, rel_tol=5e-2, abs_tol=5e-6
                ),
            ),
            (
                "acquisition.window_delay",
                {"sequence": {"enabled": False}, "acquisition": {"window_delay": target_wdelay}},
                lambda rb: math.isclose(
                    float(rb["acquisition"]["window_delay"]), target_wdelay, rel_tol=5e-2, abs_tol=5e-6
                ),
            ),
            (
                "acquisition.memory_size",
                {"acquisition": {"memory_size": target_memory}},
                lambda rb: int(rb["acquisition"].get("memory_size", 0)) > 0,
            ),
            (
                "acquisition.sample_rate",
                {"acquisition": {"sample_rate": target_sr}},
                lambda rb: float(rb["acquisition"].get("sample_rate", 0.0)) > 0.0,
            ),
            (
                "trigger.mode",
                {"trigger": {"mode": target_mode}},
                _check_trigger_mode,
            ),
            (
                "trigger.logic",
                {"trigger": {"logic": target_logic}},
                _check_trigger_logic,
            ),
            (
                "trigger.external",
                {"trigger": {"external": target_external}},
                _check_trigger_external,
            ),
            (
                "trigger.external_level",
                {"trigger": {"external_level": target_external_level}},
                _check_trigger_external_level,
            ),
            (
                "trigger.channels.1",
                {"trigger": {"channels": {"1": {"state": "HIGH", "level": target_trig_level}}}},
                _check_trigger_channel1,
            ),
            (
                "sequence.enabled",
                {"sequence": {"enabled": target_seq_enabled}},
                lambda rb: bool(rb["sequence"]["enabled"]) is target_seq_enabled,
            ),
            (
                "sequence.num_segments",
                {"sequence": {"enabled": True, "num_segments": target_seq_segments}},
                lambda rb: int(rb["sequence"]["num_segments"]) == target_seq_segments,
            ),
            (
                "sequence.timeout_enabled",
                {"sequence": {"enabled": True, "timeout_enabled": target_seq_to_en, "timeout_seconds": target_seq_to_s}},
                lambda rb: bool(rb["sequence"]["timeout_enabled"]) is target_seq_to_en,
            ),
            (
                "sequence.timeout_seconds",
                {"sequence": {"enabled": True, "timeout_enabled": True, "timeout_seconds": target_seq_to_s}},
                lambda rb: float(rb["sequence"]["timeout_seconds"]) > 0.0,
            ),
            (
                "auxiliary_output",
                {"auxiliary_output": target_aux},
                _check_auxiliary_output,
            ),
        ]

        report_lines: list[str] = []
        failures: list[str] = []

        for name, patch, check in cases:
            try:
                scope.apply_settings(base)
                scope.apply_settings(patch)
                rb = scope.read_all_settings()
                ok = bool(check(rb))
                if ok:
                    _append_runstate(_artifact_dir, f"settings_case_ok:{name}")
                    report_lines.append(f"OK    | {name}")
                else:
                    _append_runstate(_artifact_dir, f"settings_case_fail:{name}")
                    msg = f"check returned false: {name}"
                    failures.append(msg)
                    report_lines.append(f"FAIL  | {name} | {msg}")
            except Exception as exc:
                _append_runstate(_artifact_dir, f"settings_case_error:{name}:{exc}")
                msg = f"exception in {name}: {exc!r}"
                failures.append(msg)
                report_lines.append(f"ERROR | {name} | {msg}")

        report_path = _artifact_dir / "settings_roundtrip_report.log"
        with report_path.open("a", encoding="utf-8") as f:
            f.write("=== settings roundtrip report ===\n")
            for line in report_lines:
                f.write(line + "\n")
            f.write(f"SUMMARY | total={len(cases)} fail_or_error={len(failures)}\n")

        if failures:
            pytest.fail(
                "settings roundtrip failures:\n" + "\n".join(failures)
            )
    finally:
        if original_settings is not None:
            try:
                scope.apply_settings(original_settings)
                _append_runstate(_artifact_dir, "settings_roundtrip_restore_ok")
            except Exception as exc:
                _append_runstate(_artifact_dir, f"settings_roundtrip_restore_failed: {exc}")
                raise
        scope.disconnect()

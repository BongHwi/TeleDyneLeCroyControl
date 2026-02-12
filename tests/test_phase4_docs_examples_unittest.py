import argparse
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from teledyne_lecroy import (
    AcquisitionConfig,
    ChannelConfig,
    ChannelTrigger,
    SequenceData,
    TriggerConfig,
    TriggerState,
    WaveformData,
)


class _CtxScope:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


class _ConnectScope(_CtxScope):
    def query(self, command: str) -> str:
        if command == "*IDN?":
            return "TELEDYNE,MOCK,1234,1.0"
        return "OK"


class _SettingsScope(_CtxScope):
    def __init__(self) -> None:
        self.applied = []

    def read_all_settings(self) -> dict:
        return {
            "channels": {"1": {"enabled": True, "vdiv": 0.2, "offset": 0.0, "coupling": "DC50"}},
            "acquisition": {"tdiv": 1e-3, "sampling_period": 1e-6, "trigger_delay": 0.0, "window_delay": 0.0},
            "trigger": {"channels": {}, "mode": "SINGLE", "external": False, "external_level": 1.25},
            "sequence": {"enabled": False, "num_segments": 1, "timeout_enabled": False, "timeout_seconds": 10.0},
            "auxiliary_output": "TRIGGER_OUT",
        }

    def apply_settings(self, settings: dict) -> None:
        self.applied.append(settings)


class _SingleCaptureScope(_CtxScope):
    def __init__(self) -> None:
        self.mode = None
        self.armed = False

    def apply_settings(self, settings: dict) -> None:
        return None

    def read_all_settings(self) -> dict:
        return {
            "channels": {
                "1": {"enabled": True},
                "2": {"enabled": False},
            },
            "sequence": {"timeout_seconds": 10.0},
        }

    def set_trigger_mode(self, mode: str) -> None:
        self.mode = mode

    def arm(self) -> None:
        self.armed = True

    def wait_for_trigger(self, timeout: float | None = None, force: bool = False) -> None:
        return None

    def readout(self, channels=None):
        wf = WaveformData(
            raw_data=bytes([0, 1, 255]),
            channel=1,
            dx=1.0,
            x0=0.0,
            dy=1.0,
            y0=0.0,
        )
        return {1: wf}


class _SequenceScope(_CtxScope):
    def __init__(self) -> None:
        self.mode = None

    def apply_settings(self, settings: dict) -> None:
        return None

    def read_all_settings(self) -> dict:
        return {
            "channels": {
                "1": {"enabled": True},
            },
            "sequence": {"timeout_seconds": 5.0},
        }

    def set_trigger_mode(self, mode: str) -> None:
        self.mode = mode

    def arm(self) -> None:
        return None

    def wait_for_trigger(self, timeout: float | None = None, force: bool = False) -> None:
        return None

    def readout_sequence(self, channels=None):
        seg0 = WaveformData(raw_data=bytes([0, 1]), channel=1, segment=0, dx=1.0, dy=1.0, y0=0.0)
        seg1 = WaveformData(raw_data=bytes([2, 3]), channel=1, segment=1, dx=1.0, dy=1.0, y0=0.0)
        return {1: SequenceData(segments=(seg0, seg1), channel=1)}


class Phase4DocsAndExamplesTests(unittest.TestCase):
    def test_readme_trigger_snippet_contract(self) -> None:
        channels = {
            1: ChannelConfig(vdiv=0.2, offset=0.0, enabled=True),
        }
        acquisition = AcquisitionConfig(tdiv=1e-3, sampling_period=1e-6)
        trigger = TriggerConfig(
            channels={1: ChannelTrigger(state=TriggerState.HIGH, level=0.02)},
            mode="SINGLE",
        )

        self.assertIn(1, channels)
        self.assertEqual(acquisition.tdiv, 1e-3)
        self.assertEqual(trigger.channels[1].state, TriggerState.HIGH)

    def test_example_01_connect_smoke(self) -> None:
        module = importlib.import_module("examples.01_connect.connect")
        args = argparse.Namespace(model="wavepro", address="127.0.0.1", protocol="lxi")

        with mock.patch.object(module, "parse_args", return_value=args), mock.patch.object(
            module, "make_scope", return_value=_ConnectScope()
        ):
            module.main()

    def test_example_02_manual_settings_apply_smoke(self) -> None:
        module = importlib.import_module("examples.02_manual_settings.manual_settings")
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "settings.json"
            cfg.write_text(json.dumps({"sequence": {"enabled": False}}), encoding="utf-8")
            args = argparse.Namespace(
                model="wavepro",
                address="127.0.0.1",
                protocol="lxi",
                config=cfg,
                output=Path(td) / "out.json",
                force=True,
            )
            scope = _SettingsScope()
            with mock.patch.object(module, "parse_args", return_value=args), mock.patch.object(
                module, "make_scope", return_value=scope
            ):
                module.main()

            self.assertTrue(scope.applied)

    def test_example_03_single_capture_smoke(self) -> None:
        module = importlib.import_module("examples.03_single_capture.single_capture")
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "settings.json"
            cfg.write_text(json.dumps({"trigger": {"mode": "SINGLE"}}), encoding="utf-8")
            outdir = Path(td) / "out"
            args = argparse.Namespace(
                model="wavepro",
                address="127.0.0.1",
                protocol="lxi",
                outdir=outdir,
                channels=[1],
                config=cfg,
                force=True,
                trigger_timeout=60.0,
            )
            with mock.patch.object(module, "parse_args", return_value=args), mock.patch.object(
                module, "make_scope", return_value=_SingleCaptureScope()
            ), mock.patch.object(module, "save_waveform", return_value=None), mock.patch.object(
                module, "plot_waveform", return_value=None
            ):
                module.main()

    def test_example_04_sequence_capture_smoke(self) -> None:
        module = importlib.import_module("examples.04_sequence_capture.sequence_capture")
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "settings.json"
            cfg.write_text(json.dumps({"sequence": {"timeout_seconds": 3.0}}), encoding="utf-8")
            outdir = Path(td) / "out"
            args = argparse.Namespace(
                model="wavepro",
                address="127.0.0.1",
                protocol="lxi",
                outdir=outdir,
                segments=4,
                channels=[1],
                config=cfg,
                force=True,
                wait_timeout=None,
            )
            with mock.patch.object(module, "parse_args", return_value=args), mock.patch.object(
                module, "make_scope", return_value=_SequenceScope()
            ), mock.patch.object(module, "save_sequence", return_value=None), mock.patch.object(
                module, "plot_waveform", return_value=None
            ):
                module.main()


if __name__ == "__main__":
    unittest.main()

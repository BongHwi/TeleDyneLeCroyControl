import logging
import unittest

from teledyne_lecroy_core.scope import ScopeConfigurationError, TeledyneLecroyScope


class DummyScope(TeledyneLecroyScope):
    def __init__(self) -> None:
        super().__init__(address="127.0.0.1", protocol="lxi")
        self._connected = True
        self.commands: list[str] = []
        self.fail_vbs = False
        self.runstate_reads: list[str] = []

    def write(self, command: str) -> None:  # type: ignore[override]
        if self.fail_vbs and command.strip().startswith("vbs "):
            raise RuntimeError("vbs write failed")
        self.commands.append(command)

    def query(self, command: str) -> str:  # type: ignore[override]
        if self.fail_vbs and command.strip().startswith("vbs?"):
            raise RuntimeError("vbs query failed")
        self.commands.append(command)
        if "app.Acquisition.RunState" in command and self.runstate_reads:
            return self.runstate_reads.pop(0)
        if command == "TRMD?":
            return "TRMD NORM"
        return "OK"

    def read_raw(self) -> bytes:  # type: ignore[override]
        return b""

    def _configure_display(self, display: bool = False) -> None:
        return None

    def _configure_timebase(self, config):
        return None

    def _configure_channel(self, channel: int, config):
        return None

    def _configure_sequence(self, config):
        return None

    def _setup_trigger_source(self, config):
        return None

    def _setup_trigger_level(self, config):
        return None

    def _measure_baseline(self, channel: int) -> float:
        return 0.0

    def _read_channel_data(self, channel: int, offset: int = 0, count: int = 0) -> bytes:
        return b""

    def _get_waveform_scaling(self, channel: int):
        return 1.0, 0.0, 1.0, 0.0

    def _get_trigger_time(self):
        return None

    def _read_sequence_segments(self, channel: int):
        return []

    def _auto_offset_search(self):
        return {}


class VbsPolicyTests(unittest.TestCase):
    def test_fallback_allowed_for_channel_scale(self) -> None:
        scope = DummyScope()
        scope.fail_vbs = True

        scope._vbs_write(
            "app.Acquisition.C1.VerScale = 0.1",
            operation="channel_scale",
            fallback_scpi="C1:VDIV 0.1",
        )

        self.assertIn("C1:VDIV 0.1", scope.commands)

    def test_fallback_forbidden_for_trigger_pattern_write(self) -> None:
        scope = DummyScope()
        scope.fail_vbs = True

        with self.assertRaises(ScopeConfigurationError):
            scope._vbs_write(
                'app.Acquisition.Trigger.Pattern.C1 = "H"',
                operation="trigger_pattern_write",
                fallback_scpi="TRPA C1,H",
            )


class RunStateTests(unittest.TestCase):
    def test_timeout_resolution_priority(self) -> None:
        scope = DummyScope()
        scope._settings["sequence"] = {"timeout_seconds": 7.0}

        self.assertEqual(scope._resolve_state_timeout(2.0, sequence_operation=False), 5.0)
        self.assertEqual(scope._resolve_state_timeout(None, sequence_operation=False), 7.0)
        self.assertEqual(scope._resolve_state_timeout(None, sequence_operation=True), 9.0)

    def test_set_run_state_waits_for_stable_reads(self) -> None:
        scope = DummyScope()
        scope.runstate_reads = ["RUN", "RUN", "RUN"]

        scope.set_run_state("RUN", timeout=0.5)

        self.assertTrue(any("RunState = \"Run\"" in c for c in scope.commands))


class VbsLoggingTests(unittest.TestCase):
    def test_vbs_log_masks_ip_and_path(self) -> None:
        scope = DummyScope()
        logger = logging.getLogger("teledyne_lecroy.vbs")

        records: list[str] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record.getMessage())

        handler = CaptureHandler()
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            scope._log_vbs_exchange(
                1,
                "vbs? 'return=/home/user/data 192.168.0.10'",
                "reply from 10.0.0.1 in /tmp/out",
                1.5,
            )
        finally:
            logger.removeHandler(handler)

        self.assertTrue(records)
        self.assertIn("<ip>", records[0])
        self.assertIn("<path>", records[0])


if __name__ == "__main__":
    unittest.main()

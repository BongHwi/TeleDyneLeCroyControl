import unittest
from unittest import mock

from teledyne_lecroy import (
    AcquisitionConfig,
    ChannelConfig,
    ChannelTrigger,
    ScopeConfigurationError,
    SequenceConfig,
    TriggerConfig,
    TriggerState,
    WavePro,
    WaveRunner,
    WaveformData,
)
import teledyne_lecroy_core.scope as core_scope


class _FakeTransport:
    def __init__(self, malformed_block: bool = False) -> None:
        self.timeout = 30000
        self.last_write = ""
        self.writes: list[str] = []
        self.trigger_mode = "STOP"
        self.malformed_block = malformed_block

    def write(self, command: str) -> None:
        self.last_write = command.strip()
        self.writes.append(self.last_write)
        if self.last_write.startswith("TRMD "):
            self.trigger_mode = self.last_write.split()[-1]

    def query(self, command: str) -> str:
        cmd = command.strip()
        if cmd == "*IDN?":
            return "TELEDYNE,MOCK,1234,1.0"
        if cmd == "*OPC?":
            return "1"
        if cmd.startswith("TDIV?"):
            return "TDIV 1E-3 S"
        if cmd.startswith("MSIZ?"):
            return "MSIZ 1000"
        if cmd.startswith("DISP?"):
            return "DISP ON"
        if cmd.startswith("GRID?"):
            return "GRID QUATTRO"
        if cmd.startswith("BWL?"):
            return "BWL OFF"
        if cmd.startswith("TRDL?"):
            return "TRDL 0 S"
        if cmd.startswith("TRMD?"):
            return f"TRMD {self.trigger_mode}"
        if cmd.startswith("TRPA?"):
            return "TRPA C1,H,C2,X,C3,X,C4,X,EX,X,STATE,OR"
        if ":TRLV?" in cmd and cmd.startswith("C"):
            return "C1:TRLV 0.02 V"
        if cmd == "EX:TRLV?":
            return "EX:TRLV 1.25 V"
        if cmd == "SEQ?":
            return "SEQ OFF"
        if cmd.endswith(":CPL?"):
            return "C1:CPL D50"
        if cmd.endswith(":ATTN?"):
            return "C1:ATTN 1"
        if cmd.endswith(":TRA?"):
            return "C1:TRA ON"
        if cmd.endswith(":VDIV?"):
            return "C1:VDIV 0.2 V"
        if cmd.endswith(":OFST?") or cmd.endswith(":OFFSET?"):
            return "C1:OFFSET 0 V"
        if "INSPECT? 'WAVEDESC'" in cmd:
            return "\n".join([
                "WAVE_ARRAY_COUNT : 100",
                "COMM_TYPE : BYTE",
                "COMM_ORDER : LOFIRST",
            ])
        if "INSPECT? TRIGGER_TIME" in cmd:
            return "Time = 12: 34: 56.789"
        if cmd.startswith("vbs?"):
            if "RunState" in cmd:
                return "Run"
            if "SequenceTimeoutEnable" in cmd:
                return "-1"
            if "AuxOutput.AuxMode" in cmd:
                return "TriggerOut"
            if "AuxOutput.Mode" in cmd:
                return "TriggerOut"
            if "Horizontal.SampleRate" in cmd:
                return "1E4"
            if "Horizontal.NumPoints" in cmd:
                return "100"
            return "0"
        return "0"

    def read_raw(self) -> bytes:
        if self.malformed_block:
            return b"BROKEN"
        payload = bytes((i % 128 for i in range(100)))
        return b"#3100" + payload + b"\n"

    def clear(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeRM:
    def __init__(self, transport: _FakeTransport) -> None:
        self.transport = transport

    def open_resource(self, resource_name: str, resource_pyclass=None):
        _ = resource_name
        _ = resource_pyclass
        return self.transport

    def close(self) -> None:
        return None


class _FakePyVisa:
    class Error(Exception):
        pass

    def __init__(self, transport: _FakeTransport) -> None:
        self._rm = _FakeRM(transport)

    def ResourceManager(self):
        return self._rm


class WaveProContractTests(unittest.TestCase):
    def test_import_and_connect_with_fake_transport(self) -> None:
        transport = _FakeTransport()
        fake_pyvisa = _FakePyVisa(transport)

        with mock.patch.object(core_scope, "pyvisa", fake_pyvisa):
            scope = WavePro("127.0.0.1", protocol="lxi")
            scope.connect()
            self.assertTrue(scope._connected)
            self.assertIn("MOCK", scope.query("*IDN?"))
            scope.disconnect()
            self.assertFalse(scope._connected)

    def test_configure_trigger_readout_contract(self) -> None:
        scope = WavePro("127.0.0.1", protocol="lxi")
        scope._scope = _FakeTransport()
        scope._connected = True

        scope.configure(
            channels={1: ChannelConfig(vdiv=0.2, offset=0.0, enabled=True)},
            acquisition=AcquisitionConfig(tdiv=1e-3, sampling_period=1e-4),
            sequence=SequenceConfig(enabled=False),
        )
        scope.set_trigger(
            TriggerConfig(
                channels={1: ChannelTrigger(state=TriggerState.HIGH, level=0.02)},
                mode="SINGLE",
            )
        )

        data = scope.readout(channels=[1])
        self.assertIn(1, data)
        self.assertIsInstance(data[1], WaveformData)
        self.assertEqual(data[1].points, 100)

    def test_malformed_block_raises_scope_configuration_error(self) -> None:
        scope = WavePro("127.0.0.1", protocol="lxi")
        scope._scope = _FakeTransport(malformed_block=True)
        scope._connected = True
        scope._acquisition_config = AcquisitionConfig(tdiv=1e-3, sampling_period=1e-4)
        scope._channel_configs = {1: ChannelConfig(vdiv=0.2)}

        with self.assertRaises(ScopeConfigurationError):
            scope.readout(channels=[1])

    def test_waverunner_import_and_instantiation_contract(self) -> None:
        scope = WaveRunner("127.0.0.1", protocol="lxi")
        self.assertIsInstance(scope, WaveRunner)

    def test_settings_roundtrip_includes_extended_sections(self) -> None:
        transport = _FakeTransport()
        scope = WavePro("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        settings = scope.read_all_settings()
        self.assertIn("instrument", settings)
        self.assertEqual(settings["instrument"]["display"], "ON")
        self.assertEqual(settings["trigger"]["logic"], "OR")
        self.assertEqual(settings["acquisition"]["memory_size"], 1000)
        self.assertIn("attenuation", settings["channels"]["1"])

        scope.apply_settings(
            {
                "instrument": {"display": "ON", "grid": "QUATTRO", "bandwidth_limit": "OFF"},
                "channels": {
                    "1": {
                        "vdiv": 0.2,
                        "offset": 0.0,
                        "coupling": "DC50",
                        "enabled": False,
                        "attenuation": 1.0,
                    }
                },
                "acquisition": {
                    "tdiv": 1e-3,
                    "sampling_period": 1e-4,
                    "trigger_delay": 0.0,
                    "window_delay": 0.0,
                    "memory_size": 1000,
                    "sample_rate": 1e4,
                },
                "trigger": {
                    "channels": {"1": {"state": "HIGH", "level": 0.02}},
                    "mode": "SINGLE",
                    "logic": "AND",
                    "external": False,
                    "external_level": 1.25,
                },
            }
        )

        self.assertTrue(
            any(
                ('Pattern.Logic = "AND"' in cmd) or ('Pattern.LogicOperator = "And"' in cmd)
                for cmd in transport.writes
            )
        )
        self.assertTrue(any("app.Acquisition.C1.View = 0" in cmd for cmd in transport.writes))
        self.assertTrue(any(cmd.startswith("C1:ATTN 1.0") for cmd in transport.writes))
        self.assertTrue(any(cmd.startswith("MSIZ 1000") for cmd in transport.writes))

    def test_read_all_settings_parses_sequence_with_on_comma_and_aux_numeric(self) -> None:
        class _ReadbackTransport(_FakeTransport):
            def query(self, command: str) -> str:
                cmd = command.strip()
                if cmd == "SEQ?":
                    return "SEQ ON,3,2.5E+6"
                if cmd.startswith("vbs?") and "SequenceTimeoutEnable" in cmd:
                    return "1"
                if cmd.startswith("vbs?") and "AuxOutput.AuxMode" in cmd:
                    return "TriggerEnabled"
                if cmd.startswith("vbs?") and "AuxOutput.Mode" in cmd:
                    return "1"
                return super().query(command)

        scope = WavePro("127.0.0.1", protocol="lxi")
        scope._scope = _ReadbackTransport()
        scope._connected = True

        settings = scope.read_all_settings()
        self.assertEqual(settings["sequence"]["num_segments"], 3)
        self.assertTrue(settings["sequence"]["timeout_enabled"])
        self.assertEqual(settings["auxiliary_output"], "TRIGGER_ENABLED")

    def test_apply_settings_sequence_timeout_enabled_false_disables_timeout(self) -> None:
        class _TimeoutStateTransport(_FakeTransport):
            def __init__(self) -> None:
                super().__init__()
                self.sequence_timeout_enabled = True

            def write(self, command: str) -> None:
                super().write(command)
                cmd = command.strip()
                if "SequenceTimeoutEnable = -1" in cmd:
                    self.sequence_timeout_enabled = True
                if "SequenceTimeoutEnable = 0" in cmd:
                    self.sequence_timeout_enabled = False

            def query(self, command: str) -> str:
                cmd = command.strip()
                if cmd.startswith("vbs?") and "SequenceTimeoutEnable" in cmd:
                    return "-1" if self.sequence_timeout_enabled else "0"
                return super().query(command)

        transport = _TimeoutStateTransport()
        scope = WavePro("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        scope.apply_settings(
            {
                "sequence": {
                    "enabled": True,
                    "num_segments": 2,
                    "timeout_enabled": False,
                    "timeout_seconds": 10.0,
                }
            }
        )
        readback = scope.read_all_settings()
        self.assertFalse(readback["sequence"]["timeout_enabled"])

    def test_set_auxiliary_output_prefers_auxmode_property(self) -> None:
        transport = _FakeTransport()
        scope = WavePro("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        scope.set_auxiliary_output(core_scope.AuxOutputMode.TRIGGER_ENABLED)
        self.assertTrue(any("AuxOutput.AuxMode = \"TriggerEnabled\"" in cmd for cmd in transport.writes))

    def test_apply_settings_trigger_partial_keeps_existing_mode(self) -> None:
        transport = _FakeTransport()
        scope = WavePro("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True
        scope._settings["trigger"] = {"mode": "NORM", "logic": "OR", "external": False, "external_level": 1.25}

        scope.apply_settings({"trigger": {"external": True}})
        self.assertTrue(
            any(("app.Acquisition.TriggerMode = \"NORM\"" in cmd) or (cmd == "TRMD NORM") for cmd in transport.writes)
        )
        self.assertFalse(any("app.Acquisition.TriggerMode = \"SINGLE\"" in cmd for cmd in transport.writes))


if __name__ == "__main__":
    unittest.main()

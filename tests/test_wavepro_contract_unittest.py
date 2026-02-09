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
        self.trigger_mode = "STOP"
        self.malformed_block = malformed_block

    def write(self, command: str) -> None:
        self.last_write = command.strip()
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


if __name__ == "__main__":
    unittest.main()

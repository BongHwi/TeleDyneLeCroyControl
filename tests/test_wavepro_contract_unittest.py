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
    WP804HD,
    WR8208HD,
    WaveformData,
)
import teledyne_lecroy_core.scope as core_scope


class _FakeTransport:
    def __init__(
        self,
        malformed_block: bool = False,
        *,
        payload_points: int = 100,
        wavedesc_points: int = 100,
    ) -> None:
        self.timeout = 30000
        self.last_write = ""
        self.writes: list[str] = []
        self.trigger_mode = "STOP"
        self.malformed_block = malformed_block
        self.payload_points = payload_points
        self.wavedesc_points = wavedesc_points

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
                f"WAVE_ARRAY_COUNT : {self.wavedesc_points}",
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
        payload = bytes((i % 128 for i in range(self.payload_points)))
        payload_len = len(payload)
        block = f"#3{payload_len:03d}".encode("ascii") + payload
        return block + b"\n"

    def clear(self) -> None:
        return None

    def close(self) -> None:
        return None


class _MeasurementStateTransport(_FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.measurement_visible = {idx: True for idx in range(1, 13)}
        self.math_visible = {idx: True for idx in range(1, 9)}

    def write(self, command: str) -> None:
        super().write(command)
        cmd = command.strip()
        pacu_off = core_scope.re.match(r"PACU\s+(\d+),OFF", cmd, flags=core_scope.re.IGNORECASE)
        if pacu_off:
            self.measurement_visible[int(pacu_off.group(1))] = False
        f_off = core_scope.re.match(r"F(\d+):TRACE\s+OFF", cmd, flags=core_scope.re.IGNORECASE)
        if f_off:
            self.math_visible[int(f_off.group(1))] = False

        p_vbs = core_scope.re.match(
            r"vbs\s+'app\.Measure\.P(\d+)\.View\s*=\s*(true|false)'",
            cmd,
            flags=core_scope.re.IGNORECASE,
        )
        if p_vbs:
            self.measurement_visible[int(p_vbs.group(1))] = p_vbs.group(2).lower() == "true"

        f_vbs = core_scope.re.match(
            r"vbs\s+'app\.Math\.F(\d+)\.View\s*=\s*(true|false)'",
            cmd,
            flags=core_scope.re.IGNORECASE,
        )
        if f_vbs:
            self.math_visible[int(f_vbs.group(1))] = f_vbs.group(2).lower() == "true"

    def query(self, command: str) -> str:
        cmd = command.strip()
        p_q = core_scope.re.match(
            r"vbs\?\s+'return=app\.Measure\.P(\d+)\.View'",
            cmd,
            flags=core_scope.re.IGNORECASE,
        )
        if p_q:
            return "-1" if self.measurement_visible.get(int(p_q.group(1)), False) else "0"
        f_q = core_scope.re.match(
            r"vbs\?\s+'return=app\.Math\.F(\d+)\.View'",
            cmd,
            flags=core_scope.re.IGNORECASE,
        )
        if f_q:
            return "-1" if self.math_visible.get(int(f_q.group(1)), False) else "0"
        return super().query(command)


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
            scope = WP804HD("127.0.0.1", protocol="lxi")
            scope.connect()
            self.assertTrue(scope._connected)
            self.assertIn("MOCK", scope.query("*IDN?"))
            scope.disconnect()
            self.assertFalse(scope._connected)

    def test_configure_trigger_readout_contract(self) -> None:
        scope = WP804HD("127.0.0.1", protocol="lxi")
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

    def test_readout_disables_measurement_and_math_by_default(self) -> None:
        transport = _MeasurementStateTransport()
        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True
        scope._acquisition_config = AcquisitionConfig(tdiv=1e-3, sampling_period=1e-4)
        scope._channel_configs = {1: ChannelConfig(vdiv=0.2, offset=0.0, enabled=True)}

        _ = scope.readout(channels=[1])

        vis = scope.read_measurement_math_visibility(measurement_slots=2, math_traces=2)
        self.assertFalse(vis["measurement"][1])
        self.assertFalse(vis["measurement"][2])
        self.assertFalse(vis["math"][1])
        self.assertFalse(vis["math"][2])

    def test_readout_can_keep_measurement_and_math_when_requested(self) -> None:
        transport = _MeasurementStateTransport()
        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True
        scope._acquisition_config = AcquisitionConfig(tdiv=1e-3, sampling_period=1e-4)
        scope._channel_configs = {1: ChannelConfig(vdiv=0.2, offset=0.0, enabled=True)}

        _ = scope.readout(channels=[1], keep_measurement_math=True)

        vis = scope.read_measurement_math_visibility(measurement_slots=2, math_traces=2)
        self.assertTrue(vis["measurement"][1])
        self.assertTrue(vis["measurement"][2])
        self.assertTrue(vis["math"][1])
        self.assertTrue(vis["math"][2])

    def test_readout_uses_word_transfer_and_16bit_voltage_scaling(self) -> None:
        class _WordAwareTransport(_FakeTransport):
            def __init__(self) -> None:
                super().__init__()
                self.word_mode = False

            def write(self, command: str) -> None:
                super().write(command)
                if "CFMT" in command.upper() and "WORD" in command.upper():
                    self.word_mode = True

            def query(self, command: str) -> str:
                cmd = command.strip()
                if "INSPECT? 'WAVEDESC'" in cmd:
                    comm_type = "WORD" if self.word_mode else "BYTE"
                    return "\n".join(
                        [
                            f"WAVE_ARRAY_COUNT : {self.wavedesc_points}",
                            f"COMM_TYPE : {comm_type}",
                            "COMM_ORDER : LOFIRST",
                        ]
                    )
                return super().query(command)

            def read_raw(self) -> bytes:
                if self.malformed_block:
                    return b"BROKEN"
                if self.word_mode:
                    payload = b"".join(
                        int(i % 32768).to_bytes(2, byteorder="little", signed=True)
                        for i in range(self.payload_points)
                    )
                else:
                    payload = bytes((i % 128 for i in range(self.payload_points)))
                payload_len = len(payload)
                block = f"#3{payload_len:03d}".encode("ascii") + payload
                return block + b"\n"

        transport = _WordAwareTransport()
        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True
        scope._acquisition_config = AcquisitionConfig(tdiv=1e-3, sampling_period=1e-4)
        scope._channel_configs = {1: ChannelConfig(vdiv=0.2, offset=0.0, enabled=True)}

        data = scope.readout(channels=[1])

        self.assertIn(1, data)
        self.assertEqual(data[1].sample_width_bytes, 2)
        self.assertTrue(
            any("CFMT" in cmd and "WORD" in cmd for cmd in transport.writes),
            "Expected readout to force COMM_TYPE WORD transfer",
        )
        self.assertAlmostEqual(data[1].dy, 0.2 * 8.0 / 65536.0, places=12)

    def test_malformed_block_raises_scope_configuration_error(self) -> None:
        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = _FakeTransport(malformed_block=True)
        scope._connected = True
        scope._acquisition_config = AcquisitionConfig(tdiv=1e-3, sampling_period=1e-4)
        scope._channel_configs = {1: ChannelConfig(vdiv=0.2)}

        with self.assertRaises(ScopeConfigurationError):
            scope.readout(channels=[1])

    def test_sequence_readout_tolerates_payload_point_mismatch(self) -> None:
        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = _FakeTransport(payload_points=120, wavedesc_points=100)
        scope._connected = True

        scope.configure(
            channels={1: ChannelConfig(vdiv=0.2, offset=0.0, enabled=True)},
            acquisition=AcquisitionConfig(tdiv=1e-3, sampling_period=1e-4),
            sequence=SequenceConfig(enabled=True, num_segments=1),
        )

        data = scope.readout_sequence(channels=[1])
        self.assertIn(1, data)
        self.assertEqual(len(data[1]), 1)
        self.assertEqual(data[1][0].points, 100)

    def test_sequence_readout_fetches_bulk_data_once_and_splits_locally(self) -> None:
        class _BulkSequenceTransport(_FakeTransport):
            def __init__(self) -> None:
                super().__init__(payload_points=300, wavedesc_points=300)
                self._next_points = 300

            def write(self, command: str) -> None:
                super().write(command)
                match = core_scope.re.search(r"WF\? DAT1,NO,\d+,NP,(\d+)", command)
                if match:
                    self._next_points = int(match.group(1))

            def read_raw(self) -> bytes:
                payload = bytes((i % 128 for i in range(self._next_points)))
                payload_len = len(payload)
                payload_len_str = str(payload_len)
                block = (
                    f"#{len(payload_len_str)}{payload_len_str}".encode("ascii")
                    + payload
                )
                return block + b"\n"

        transport = _BulkSequenceTransport()
        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        scope.configure(
            channels={1: ChannelConfig(vdiv=0.2, offset=0.0, enabled=True)},
            acquisition=AcquisitionConfig(tdiv=1e-3, sampling_period=1e-4),
            sequence=SequenceConfig(enabled=True, num_segments=3),
        )

        data = scope.readout_sequence(channels=[1])
        self.assertIn(1, data)
        self.assertEqual(len(data[1]), 3)
        self.assertEqual([seg.points for seg in data[1].segments], [100, 100, 100])
        wf_reads = [cmd for cmd in transport.writes if "WF? DAT1" in cmd]
        self.assertEqual(len(wf_reads), 1)

    def test_sequence_readout_works_without_preconfigured_acquisition(self) -> None:
        class _BulkSequenceTransport(_FakeTransport):
            def __init__(self) -> None:
                super().__init__(payload_points=300, wavedesc_points=300)
                self._next_points = 300

            def write(self, command: str) -> None:
                super().write(command)
                match = core_scope.re.search(r"WF\? DAT1,NO,\d+,NP,(\d+)", command)
                if match:
                    self._next_points = int(match.group(1))

            def read_raw(self) -> bytes:
                payload = bytes((i % 128 for i in range(self._next_points)))
                payload_len = len(payload)
                payload_len_str = str(payload_len)
                block = (
                    f"#{len(payload_len_str)}{payload_len_str}".encode("ascii")
                    + payload
                )
                return block + b"\n"

        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = _BulkSequenceTransport()
        scope._connected = True
        scope._channel_configs = {1: ChannelConfig(vdiv=0.2, offset=0.0, enabled=True)}
        scope._sequence_config = SequenceConfig(enabled=True, num_segments=3)
        scope._acquisition_config = None

        data = scope.readout_sequence(channels=[1])
        self.assertIn(1, data)
        self.assertEqual(len(data[1]), 3)

    def test_sequence_readout_without_acquisition_splits_even_with_small_remainder(self) -> None:
        class _RemainderSequenceTransport(_FakeTransport):
            def __init__(self) -> None:
                super().__init__(payload_points=2_500_002, wavedesc_points=2_500_002)

            def query(self, command: str) -> str:
                if command.strip() == "MSIZ?":
                    return "MSIZ 2500000"
                return super().query(command)

            def read_raw(self) -> bytes:
                payload = bytes((i % 128 for i in range(self.payload_points)))
                payload_len = len(payload)
                payload_len_str = str(payload_len)
                block = (
                    f"#{len(payload_len_str)}{payload_len_str}".encode("ascii")
                    + payload
                )
                return block + b"\n"

        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = _RemainderSequenceTransport()
        scope._connected = True
        scope._channel_configs = {1: ChannelConfig(vdiv=0.2, offset=0.0, enabled=True)}
        scope._sequence_config = SequenceConfig(enabled=True, num_segments=20)
        scope._acquisition_config = None

        data = scope.readout_sequence(channels=[1])
        self.assertIn(1, data)
        self.assertEqual(len(data[1]), 20)

    def test_waverunner_import_and_instantiation_contract(self) -> None:
        scope = WR8208HD("127.0.0.1", protocol="lxi")
        self.assertIsInstance(scope, WR8208HD)

    def test_waverunner_accepts_eight_active_channels(self) -> None:
        scope = WR8208HD("127.0.0.1", protocol="lxi", active_channels=list(range(1, 9)))
        self.assertEqual(scope._active_channels, [1, 2, 3, 4, 5, 6, 7, 8])

    def test_waverunner_trigger_pattern_writes_states_for_channels_five_to_eight(self) -> None:
        transport = _FakeTransport()
        scope = WR8208HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        scope.set_trigger(
            TriggerConfig(
                channels={
                    1: ChannelTrigger(state=TriggerState.HIGH, level=0.01),
                    5: ChannelTrigger(state=TriggerState.LOW, level=0.02),
                    8: ChannelTrigger(state=TriggerState.HIGH, level=0.03),
                },
                mode="SINGLE",
            )
        )

        self.assertTrue(
            any(("TRPA " in cmd and "C5,L" in cmd) for cmd in transport.writes)
        )
        self.assertTrue(
            any(("TRPA " in cmd and "C8,H" in cmd) for cmd in transport.writes)
        )

    def test_waverunner_sequence_batch_uses_word_transfer_and_16bit_scaling(self) -> None:
        class _WordAwareSequenceTransport(_FakeTransport):
            def __init__(self) -> None:
                super().__init__(payload_points=100, wavedesc_points=200)
                self.word_mode = False
                self._next_points = 100

            def write(self, command: str) -> None:
                super().write(command)
                if "CFMT" in command.upper() and "WORD" in command.upper():
                    self.word_mode = True
                match = core_scope.re.search(r"WF\? DAT1,NO,\d+,NP,(\d+)", command)
                if match:
                    self._next_points = int(match.group(1))

            def query(self, command: str) -> str:
                if "INSPECT? 'WAVEDESC'" in command.strip():
                    comm_type = "WORD" if self.word_mode else "BYTE"
                    return "\n".join(
                        [
                            f"WAVE_ARRAY_COUNT : {self.wavedesc_points}",
                            f"COMM_TYPE : {comm_type}",
                            "COMM_ORDER : LOFIRST",
                        ]
                    )
                return super().query(command)

            def read_raw(self) -> bytes:
                if self.word_mode:
                    payload = b"".join(
                        int(i % 32768).to_bytes(2, byteorder="little", signed=True)
                        for i in range(self._next_points)
                    )
                else:
                    payload = bytes((i % 128 for i in range(self._next_points)))
                payload_len_str = str(len(payload))
                block = f"#{len(payload_len_str)}{payload_len_str}".encode("ascii") + payload
                return block + b"\n"

        transport = _WordAwareSequenceTransport()
        scope = WR8208HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True
        scope._acquisition_config = AcquisitionConfig(tdiv=1e-3, sampling_period=1e-4)
        scope._channel_configs = {1: ChannelConfig(vdiv=0.2, offset=0.0, enabled=True)}
        scope._sequence_config = SequenceConfig(enabled=True, num_segments=2)

        data = scope.readout_sequence(channels=[1], sn_mode="batch", batch_segments=1)

        self.assertIn(1, data)
        self.assertEqual(len(data[1]), 2)
        self.assertEqual(data[1][0].sample_width_bytes, 2)
        self.assertTrue(
            any("CFMT" in cmd and "WORD" in cmd for cmd in transport.writes),
            "Expected sequence readout to force COMM_TYPE WORD transfer",
        )
        self.assertAlmostEqual(data[1][0].dy, 0.2 * 8.0 / 65536.0, places=12)

    def test_wavepro_trigger_pattern_always_writes_full_channel_state_map(self) -> None:
        transport = _FakeTransport()
        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        scope.set_trigger(
            TriggerConfig(
                channels={1: ChannelTrigger(state=TriggerState.HIGH, level=0.02)},
                mode="SINGLE",
            )
        )

        self.assertTrue(
            any(
                cmd.startswith("TRPA C1,H,C2,X,C3,X,C4,X,STATE,OR")
                for cmd in transport.writes
            )
        )
        self.assertIn("TRSE EDGE,SR,C1", transport.writes)
        self.assertIn("TRSL POS", transport.writes)

    def test_wavepro_trigger_pattern_sets_negative_slope_for_low_state(self) -> None:
        transport = _FakeTransport()
        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        scope.set_trigger(
            TriggerConfig(
                channels={1: ChannelTrigger(state=TriggerState.LOW, level=0.02)},
                mode="SINGLE",
            )
        )

        self.assertIn("TRSE EDGE,SR,C1", transport.writes)
        self.assertIn("TRSL NEG", transport.writes)

    def test_wavepro_external_only_trigger_uses_trse_without_trpa(self) -> None:
        transport = _FakeTransport()
        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        scope.set_trigger(
            TriggerConfig(
                channels={},
                external=True,
                mode="SINGLE",
            )
        )

        self.assertTrue(any(cmd.startswith("TRSE EDGE,SR,EX") for cmd in transport.writes))
        self.assertFalse(any(cmd.startswith("TRPA ") for cmd in transport.writes))

    def test_waverunner_trigger_pattern_always_writes_full_channel_state_map(self) -> None:
        transport = _FakeTransport()
        scope = WR8208HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        scope.set_trigger(
            TriggerConfig(
                channels={
                    1: ChannelTrigger(state=TriggerState.HIGH, level=0.01),
                    5: ChannelTrigger(state=TriggerState.LOW, level=0.02),
                    8: ChannelTrigger(state=TriggerState.HIGH, level=0.03),
                },
                mode="SINGLE",
            )
        )

        self.assertTrue(
            any(
                cmd.startswith(
                    "TRPA C1,H,C2,X,C3,X,C4,X,C5,L,C6,X,C7,X,C8,H,STATE,OR"
                )
                for cmd in transport.writes
            )
        )
        self.assertTrue(any(cmd.startswith("TRSE EDGE,SR,C1") for cmd in transport.writes))
        self.assertIn("TRSL POS", transport.writes)

    def test_waverunner_trigger_pattern_sets_negative_slope_for_low_state(self) -> None:
        transport = _FakeTransport()
        scope = WR8208HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        scope.set_trigger(
            TriggerConfig(
                channels={5: ChannelTrigger(state=TriggerState.LOW, level=0.02)},
                mode="SINGLE",
            )
        )

        self.assertIn("TRSE EDGE,SR,C5", transport.writes)
        self.assertIn("TRSL NEG", transport.writes)

    def test_waverunner_external_only_trigger_falls_back_when_trpa_rejected(self) -> None:
        class _RejectExternalOnlyTrpaTransport(_FakeTransport):
            def write(self, command: str) -> None:
                cmd = command.strip()
                if cmd.startswith("TRPA ") and ",EX,H," in cmd:
                    raise RuntimeError("parameter cannot be interpreted by this command")
                super().write(command)

        transport = _RejectExternalOnlyTrpaTransport()
        scope = WR8208HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        scope.set_trigger(
            TriggerConfig(
                channels={},
                external=True,
                mode="SINGLE",
            )
        )

        self.assertTrue(any(cmd.startswith("TRSE EDGE,SR,EX") for cmd in transport.writes))

    def test_waverunner_external_only_trigger_uses_trse_without_trpa(self) -> None:
        transport = _FakeTransport()
        scope = WR8208HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        scope.set_trigger(
            TriggerConfig(
                channels={},
                external=True,
                mode="SINGLE",
            )
        )

        self.assertTrue(any(cmd.startswith("TRSE EDGE,SR,EX") for cmd in transport.writes))
        self.assertFalse(any(cmd.startswith("TRPA ") for cmd in transport.writes))

    def test_relative_trigger_level_measurement_is_cleaned_up(self) -> None:
        transport = _FakeTransport()
        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        scope.set_trigger(
            TriggerConfig(
                channels={
                    1: ChannelTrigger(state=TriggerState.HIGH, level=None, level_offset=0.01),
                },
                mode="SINGLE",
            )
        )

        self.assertIn("PACU 1,MEAN,C1", transport.writes)
        self.assertIn("vbs 'app.Measure.P1.View = false'", transport.writes)

    def test_auto_offset_search_measurement_is_cleaned_up(self) -> None:
        class _AutoOffsetTransport(_FakeTransport):
            def query(self, command: str) -> str:
                cmd = command.strip()
                if "app.measure.p2.out.result.value" in cmd:
                    return "0.02"
                if "app.measure.p1.out.result.value" in cmd:
                    return "0.00"
                return super().query(command)

        transport = _AutoOffsetTransport()
        scope = WP804HD("127.0.0.1", protocol="lxi", active_channels=[1])
        scope._scope = transport
        scope._connected = True
        scope._channel_configs = {1: ChannelConfig(vdiv=0.2, offset=0.0, enabled=True)}

        with mock.patch.object(core_scope.time, "sleep", return_value=None):
            _ = scope.set_offset(search=True)

        self.assertIn("PACU 1,MEAN,C1", transport.writes)
        self.assertIn("PACU 2,AMPL,C1", transport.writes)
        self.assertIn("vbs 'app.Measure.P1.View = false'", transport.writes)
        self.assertIn("vbs 'app.Measure.P2.View = false'", transport.writes)

    def test_waverunner_sequence_readout_uses_local_scaling_without_extra_queries(self) -> None:
        class _QueryTrackingTransport(_FakeTransport):
            def __init__(self) -> None:
                super().__init__(payload_points=40, wavedesc_points=40)
                self.queries: list[str] = []

            def query(self, command: str) -> str:
                self.queries.append(command.strip())
                return super().query(command)

        transport = _QueryTrackingTransport()
        scope = WR8208HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True
        scope._acquisition_config = AcquisitionConfig(tdiv=1e-9, sampling_period=1e-10, trigger_delay=0.0)
        scope._sequence_config = SequenceConfig(enabled=True, num_segments=4)
        scope._channel_configs = {1: ChannelConfig(vdiv=0.1, offset=0.0, enabled=True)}

        data = scope.readout_sequence(channels=[1])
        self.assertIn(1, data)

        qlog = ",".join(transport.queries)
        self.assertNotIn("TDIV?", qlog)
        self.assertNotIn("TRDL?", qlog)
        self.assertNotIn("C1:VDIV?", qlog)
        self.assertNotIn("C1:OFFSET?", qlog)

    def test_waverunner_sequence_readout_loop_mode_uses_sn_per_segment(self) -> None:
        transport = _FakeTransport(payload_points=10, wavedesc_points=10)
        scope = WR8208HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True
        scope._acquisition_config = AcquisitionConfig(tdiv=1e-9, sampling_period=1e-10, trigger_delay=0.0)
        scope._sequence_config = SequenceConfig(enabled=True, num_segments=3)
        scope._channel_configs = {1: ChannelConfig(vdiv=0.1, offset=0.0, enabled=True)}

        data = scope.readout_sequence(channels=[1], sn_mode="loop")
        self.assertIn(1, data)
        self.assertEqual(len(data[1]), 3)
        self.assertTrue(any("SN,1" in cmd for cmd in transport.writes))
        self.assertTrue(any("SN,2" in cmd for cmd in transport.writes))
        self.assertTrue(any("SN,3" in cmd for cmd in transport.writes))

    def test_waverunner_sequence_readout_batch_mode_uses_offset_reads(self) -> None:
        transport = _FakeTransport(payload_points=100, wavedesc_points=100)
        scope = WR8208HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True
        scope._acquisition_config = AcquisitionConfig(tdiv=1e-9, sampling_period=1e-10, trigger_delay=0.0)
        scope._sequence_config = SequenceConfig(enabled=True, num_segments=3)
        scope._channel_configs = {1: ChannelConfig(vdiv=0.1, offset=0.0, enabled=True)}

        data = scope.readout_sequence(channels=[1], sn_mode="batch", batch_segments=1)
        self.assertIn(1, data)
        self.assertGreaterEqual(len(data[1]), 1)
        self.assertTrue(any("WF? DAT1,NO,0,NP,100" in cmd for cmd in transport.writes))
        self.assertIn("vbs 'app.Measure.P1.View = false'", transport.writes)
        self.assertIn("F1:TRACE OFF", transport.writes)

    def test_waverunner_sequence_readout_auto_prefers_batch_for_large_segment_count(self) -> None:
        transport = _FakeTransport(payload_points=10, wavedesc_points=10)
        scope = WR8208HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True
        scope._acquisition_config = AcquisitionConfig(tdiv=1e-9, sampling_period=1e-10, trigger_delay=0.0)
        scope._sequence_config = SequenceConfig(enabled=True, num_segments=1500)
        scope._channel_configs = {1: ChannelConfig(vdiv=0.1, offset=0.0, enabled=True)}

        _ = scope.readout_sequence(channels=[1], sn_mode="auto", batch_segments=100)
        self.assertTrue(any("WF? DAT1,NO," in cmd for cmd in transport.writes))

    def test_waverunner_sequence_profile_contains_channel_metrics(self) -> None:
        transport = _FakeTransport(payload_points=100, wavedesc_points=100)
        scope = WR8208HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True
        scope._acquisition_config = AcquisitionConfig(tdiv=1e-9, sampling_period=1e-10, trigger_delay=0.0)
        scope._sequence_config = SequenceConfig(enabled=True, num_segments=3)
        scope._channel_configs = {1: ChannelConfig(vdiv=0.1, offset=0.0, enabled=True)}

        _ = scope.readout_sequence(channels=[1], sn_mode="batch", batch_segments=1)
        prof = scope.get_last_sequence_profile()
        self.assertIsNotNone(prof)
        assert prof is not None
        self.assertEqual(prof["mode"], "batch")
        self.assertIn(1, prof["channel_metrics"])
        self.assertIn("transfer_ms", prof["channel_metrics"][1])

    def test_clear_sweeps_sends_clsw_command(self) -> None:
        transport = _FakeTransport()
        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        scope.clear_sweeps()
        self.assertEqual(transport.last_write, "CLSW")

    def test_set_trigger_mode_keeps_arm_mode_consistent(self) -> None:
        transport = _FakeTransport()
        scope = WR8208HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        scope.set_trigger(
            TriggerConfig(
                channels={1: ChannelTrigger(state=TriggerState.HIGH, level=0.01)},
                mode="NORM",
            )
        )
        scope.set_trigger_mode("SINGLE")
        scope.arm(force=False)

        trmd_writes = [cmd for cmd in transport.writes if cmd.startswith("TRMD ")]
        self.assertIn("TRMD SINGLE", trmd_writes)
        self.assertFalse(any(cmd == "TRMD NORM" for cmd in trmd_writes[-1:]))

    def test_clear_all_memory_sends_vbs_command(self) -> None:
        transport = _FakeTransport()
        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        scope.clear_all_memory()
        # VBS command format is: vbs 'expression'
        self.assertIn("app.Memory.ClearAllMem", transport.last_write)
        self.assertTrue(transport.last_write.startswith("vbs '"))

    def test_settings_roundtrip_includes_extended_sections(self) -> None:
        transport = _FakeTransport()
        scope = WP804HD("127.0.0.1", protocol="lxi")
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
                ("TRPA " in cmd and "STATE,AND" in cmd)
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

        scope = WP804HD("127.0.0.1", protocol="lxi")
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
        scope = WP804HD("127.0.0.1", protocol="lxi")
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
        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        scope.set_auxiliary_output(core_scope.AuxOutputMode.TRIGGER_ENABLED)
        self.assertTrue(any("AuxOutput.AuxMode = \"TriggerEnabled\"" in cmd for cmd in transport.writes))

    def test_apply_settings_trigger_partial_keeps_existing_mode(self) -> None:
        transport = _FakeTransport()
        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True
        scope._settings["trigger"] = {"mode": "NORM", "logic": "OR", "external": False, "external_level": 1.25}

        scope.apply_settings({"trigger": {"external": True}})
        self.assertTrue(
            any(("app.Acquisition.TriggerMode = \"NORM\"" in cmd) or (cmd == "TRMD NORM") for cmd in transport.writes)
        )
        self.assertFalse(any("app.Acquisition.TriggerMode = \"SINGLE\"" in cmd for cmd in transport.writes))

    def test_configure_clamps_sample_rate_when_four_channels_active(self) -> None:
        transport = _FakeTransport()
        scope = WP804HD(
            "127.0.0.1",
            protocol="lxi",
            active_channels=[1, 2, 3, 4],
        )
        scope._scope = transport
        scope._connected = True

        with self.assertLogs("WP804HD", level="WARNING") as logs:
            scope.configure(
                acquisition=AcquisitionConfig(
                    tdiv=10e-9,
                    sampling_period=50e-12,  # 20 GS/s request
                )
            )

        self.assertAlmostEqual(scope._acquisition_config.sampling_period, 100e-12)
        self.assertTrue(any("sample rate" in line.lower() for line in logs.output))

    def test_set_maximum_memory_mode_does_not_force_sample_rate_write(self) -> None:
        transport = _FakeTransport()
        scope = WP804HD("127.0.0.1", protocol="lxi", active_channels=[1, 2, 3, 4])
        scope._scope = transport
        scope._connected = True

        scope.configure(
            acquisition=AcquisitionConfig(
                tdiv=10e-9,
                sampling_period=100e-12,
                max_samples=500,
                acquisition_mode="set_maximum_memory",
            )
        )

        self.assertFalse(
            any("Horizontal.SampleRate =" in cmd for cmd in transport.writes)
        )
        self.assertTrue(any(cmd == "MSIZ 500" for cmd in transport.writes))
        assert scope._acquisition_config is not None
        self.assertEqual(scope._acquisition_config.acquisition_mode, "set_maximum_memory")
        self.assertEqual(scope._acquisition_config.max_samples, 500)

    def test_configure_clamps_tdiv_for_sequence_memory_limit(self) -> None:
        transport = _FakeTransport()
        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        scope.configure(sequence=SequenceConfig(enabled=True, num_segments=500))

        with self.assertLogs("WP804HD", level="WARNING") as logs:
            scope.configure(
                acquisition=AcquisitionConfig(
                    tdiv=200e-9,
                    sampling_period=100e-12,  # 10 GS/s
                )
            )

        self.assertAlmostEqual(scope._acquisition_config.tdiv, 200e-9)
        self.assertAlmostEqual(scope._acquisition_config.sampling_period, 200e-12)
        self.assertTrue(any("sampling period" in line.lower() for line in logs.output))

    def test_configure_readback_shows_clamped_values_not_requested_values(self) -> None:
        transport = _FakeTransport()
        scope = WP804HD(
            "127.0.0.1",
            protocol="lxi",
            active_channels=[1, 2, 3, 4],
        )
        scope._scope = transport
        scope._connected = True

        requested_tdiv = 50e-9
        requested_sampling_period = 50e-12  # 20 GS/s (invalid for 4ch)
        scope.configure(sequence=SequenceConfig(enabled=True, num_segments=500))
        scope.configure(
            acquisition=AcquisitionConfig(
                tdiv=requested_tdiv,
                sampling_period=requested_sampling_period,
            )
        )

        rb = scope.settings["acquisition"]
        self.assertEqual(rb["tdiv"], requested_tdiv)
        self.assertNotEqual(rb["sampling_period"], requested_sampling_period)
        self.assertAlmostEqual(rb["sampling_period"], 100e-12)

    def test_wp804hd_4ch_10kseg_keeps_100ps_at_1ns_div(self) -> None:
        transport = _FakeTransport()
        scope = WP804HD("127.0.0.1", protocol="lxi", active_channels=[1, 2, 3, 4])
        scope._scope = transport
        scope._connected = True
        scope.configure(sequence=SequenceConfig(enabled=True, num_segments=10_000))

        scope.configure(
            acquisition=AcquisitionConfig(
                tdiv=1e-9,
                sampling_period=100e-12,
            )
        )

        assert scope._acquisition_config is not None
        self.assertAlmostEqual(scope._acquisition_config.sampling_period, 100e-12)

    def test_configure_clamps_window_delay_after_tdiv_clamp(self) -> None:
        transport = _FakeTransport()
        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True
        scope.configure(sequence=SequenceConfig(enabled=True, num_segments=5000))

        with self.assertLogs("WP804HD", level="WARNING") as logs:
            scope.configure(
                acquisition=AcquisitionConfig(
                    tdiv=1e-5,
                    sampling_period=100e-12,
                    window_delay=1e-3,
                )
            )

        assert scope._acquisition_config is not None
        max_window = scope.TIME_DIVISIONS / 2 * scope._acquisition_config.tdiv
        self.assertAlmostEqual(scope._acquisition_config.window_delay, max_window)
        self.assertTrue(any("window_delay" in line.lower() for line in logs.output))

    def test_configure_applies_user_max_samples_cap(self) -> None:
        transport = _FakeTransport()
        scope = WP804HD("127.0.0.1", protocol="lxi")
        scope._scope = transport
        scope._connected = True

        scope.configure(
            acquisition=AcquisitionConfig(
                tdiv=10e-9,
                sampling_period=100e-12,  # 1000 points over 100 ns span
                max_samples=500,
            )
        )

        assert scope._acquisition_config is not None
        self.assertAlmostEqual(scope._acquisition_config.sampling_period, 200e-12)
        self.assertEqual(scope._acquisition_config.max_samples, 500)
        rb = scope.settings["acquisition"]
        self.assertEqual(rb["max_samples"], 500)

    def test_configure_clamps_user_max_samples_to_model_limit(self) -> None:
        transport = _FakeTransport()
        scope = WR8208HD("127.0.0.1", protocol="lxi", active_channels=list(range(1, 9)))
        scope._scope = transport
        scope._connected = True
        scope.configure(sequence=SequenceConfig(enabled=True, num_segments=5000))

        with self.assertLogs("WR8208HD", level="WARNING") as logs:
            scope.configure(
                acquisition=AcquisitionConfig(
                    tdiv=1e-6,
                    sampling_period=100e-12,
                    max_samples=20_000,  # > per-segment limit 10,000 for 8ch/5000seg
                )
            )

        assert scope._acquisition_config is not None
        self.assertEqual(scope._acquisition_config.max_samples, 10_000)
        self.assertTrue(any("max_samples" in line.lower() for line in logs.output))

    def test_wr8208hd_8ch_5000seg_keeps_100ps_at_100ns_div(self) -> None:
        transport = _FakeTransport()
        scope = WR8208HD("127.0.0.1", protocol="lxi", active_channels=list(range(1, 9)))
        scope._scope = transport
        scope._connected = True
        scope.configure(sequence=SequenceConfig(enabled=True, num_segments=5000))
        scope.configure(
            acquisition=AcquisitionConfig(
                tdiv=100e-9,
                sampling_period=100e-12,
            )
        )
        assert scope._acquisition_config is not None
        self.assertAlmostEqual(scope._acquisition_config.tdiv, 100e-9)
        self.assertAlmostEqual(scope._acquisition_config.sampling_period, 100e-12)

    def test_wr8208hd_8ch_5000seg_raises_to_200ps_at_200ns_div(self) -> None:
        transport = _FakeTransport()
        scope = WR8208HD("127.0.0.1", protocol="lxi", active_channels=list(range(1, 9)))
        scope._scope = transport
        scope._connected = True
        scope.configure(sequence=SequenceConfig(enabled=True, num_segments=5000))
        with self.assertLogs("WR8208HD", level="WARNING") as logs:
            scope.configure(
                acquisition=AcquisitionConfig(
                    tdiv=200e-9,
                    sampling_period=100e-12,
                )
            )
        assert scope._acquisition_config is not None
        self.assertAlmostEqual(scope._acquisition_config.tdiv, 200e-9)
        self.assertAlmostEqual(scope._acquisition_config.sampling_period, 200e-12)
        self.assertTrue(any("sampling period" in line.lower() for line in logs.output))


if __name__ == "__main__":
    unittest.main()

import unittest

import numpy as np

from teledyne_lecroy_core.block_parser import parse_ieee4882_block
from teledyne_lecroy_core.scope import WaveformData


class BlockParserTests(unittest.TestCase):
    def test_parse_8bit_signed_waveform(self) -> None:
        payload = bytes([0, 127, 255])
        block = b"#13" + payload

        parsed = parse_ieee4882_block(block, sample_width_bytes=1)

        self.assertEqual(parsed.payload, payload)
        self.assertEqual(parsed.points, 3)

        wf = WaveformData(raw_data=parsed.payload, channel=1, dy=1.0, y0=0.0)
        np.testing.assert_array_equal(wf.to_voltage(), np.array([0.0, 127.0, -1.0]))

    def test_parse_16bit_waveform_little_endian(self) -> None:
        samples = np.array([1, -2, 300], dtype="<i2")
        payload = samples.tobytes()
        block = f"#2{len(payload):02d}".encode("ascii") + payload

        parsed = parse_ieee4882_block(
            block,
            sample_width_bytes=2,
            byte_order="little",
        )

        self.assertEqual(parsed.points, 3)
        wf = WaveformData(
            raw_data=parsed.payload,
            channel=1,
            dy=1.0,
            y0=0.0,
            sample_width_bytes=2,
            byte_order="little",
            points=parsed.points,
        )
        np.testing.assert_array_equal(wf.to_voltage(), np.array([1.0, -2.0, 300.0]))

    def test_parse_16bit_waveform_big_endian(self) -> None:
        samples = np.array([1, -2, 300], dtype=">i2")
        payload = samples.tobytes()
        block = f"#2{len(payload):02d}".encode("ascii") + payload

        parsed = parse_ieee4882_block(
            block,
            sample_width_bytes=2,
            byte_order="big",
        )

        self.assertEqual(parsed.points, 3)
        wf = WaveformData(
            raw_data=parsed.payload,
            channel=1,
            dy=1.0,
            y0=0.0,
            sample_width_bytes=2,
            byte_order="big",
            points=parsed.points,
        )
        np.testing.assert_array_equal(wf.to_voltage(), np.array([1.0, -2.0, 300.0]))

    def test_truncated_block_raises(self) -> None:
        # Header claims 10 bytes, payload only 3 bytes.
        block = b"#210abc"
        with self.assertRaises(ValueError):
            parse_ieee4882_block(block, sample_width_bytes=1)

    def test_point_mismatch_raises(self) -> None:
        payload = bytes([1, 2, 3, 4])
        block = b"#14" + payload

        with self.assertRaises(ValueError):
            parse_ieee4882_block(block, sample_width_bytes=1, expected_points=5)

    def test_transport_equivalent_payload(self) -> None:
        payload = bytes([10, 20, 30, 40])
        lxi_bytes = b"#14" + payload
        vicp_bytes = b"#14" + payload

        lxi = parse_ieee4882_block(lxi_bytes, sample_width_bytes=1)
        vicp = parse_ieee4882_block(vicp_bytes, sample_width_bytes=1)

        self.assertEqual(lxi.payload, vicp.payload)
        self.assertEqual(lxi.points, vicp.points)


if __name__ == "__main__":
    unittest.main()

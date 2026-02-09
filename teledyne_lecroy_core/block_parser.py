"""Transport-agnostic IEEE 488.2 block parser utilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ParsedBlock:
    """Parsed waveform block payload and decoding metadata."""

    payload: bytes
    sample_width_bytes: Literal[1, 2]
    byte_order: Literal["little", "big"]
    points: int


def parse_ieee4882_block(
    data: bytes,
    *,
    sample_width_bytes: Literal[1, 2] = 1,
    byte_order: Literal["little", "big"] = "little",
    expected_points: int | None = None,
) -> ParsedBlock:
    """Parse an IEEE 488.2 definite/indefinite-length block from bytes."""
    if sample_width_bytes not in (1, 2):
        raise ValueError(f"Unsupported sample width: {sample_width_bytes}")
    if byte_order not in ("little", "big"):
        raise ValueError(f"Unsupported byte order: {byte_order}")

    try:
        hash_idx = data.index(b"#")
    except ValueError as exc:
        raise ValueError("Missing IEEE488.2 block header") from exc

    if hash_idx + 1 >= len(data):
        raise ValueError("Incomplete IEEE488.2 block header")

    n_digits_raw = data[hash_idx + 1 : hash_idx + 2].decode("ascii")
    if not n_digits_raw.isdigit():
        raise ValueError("Invalid IEEE488.2 length digit")
    n_digits = int(n_digits_raw)

    if n_digits == 0:
        payload = data[hash_idx + 2 :]
        if payload.endswith(b"\n"):
            payload = payload[:-1]
    else:
        length_start = hash_idx + 2
        length_end = length_start + n_digits
        if length_end > len(data):
            raise ValueError("Truncated IEEE488.2 length field")
        length_str = data[length_start:length_end].decode("ascii")
        if not length_str.isdigit():
            raise ValueError("Invalid IEEE488.2 payload length field")
        payload_size = int(length_str)
        payload_start = length_end
        payload_end = payload_start + payload_size
        if payload_end > len(data):
            raise ValueError("Truncated IEEE488.2 payload")
        payload = data[payload_start:payload_end]

    if len(payload) % sample_width_bytes != 0:
        raise ValueError(
            "Payload size is not aligned to sample width "
            f"({len(payload)} bytes for {sample_width_bytes}-byte samples)"
        )

    points = len(payload) // sample_width_bytes
    if expected_points is not None and points != expected_points:
        raise ValueError(
            f"Point count mismatch: expected {expected_points}, got {points}"
        )

    return ParsedBlock(
        payload=payload,
        sample_width_bytes=sample_width_bytes,
        byte_order=byte_order,
        points=points,
    )

#!/usr/bin/env python3
"""
Teledyne LeCroy oscilloscope library.

Supports WP804HD and WR8208HD series oscilloscopes via VISA/TCP.

Bong-Hwi Lim (UTokyo)
"""
from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from itertools import count
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from .block_parser import parse_ieee4882_block

try:
    import pyvisa
    from pyvisa.resources import MessageBasedResource
except ModuleNotFoundError:  # pragma: no cover - exercised in minimal CI envs
    pyvisa = None
    MessageBasedResource = Any


__all__ = [
    # Enums
    "TriggerSlope",
    "TriggerState",
    "Coupling",
    "AuxOutputMode",
    # Constants
    "SETTINGS_OPTIONS",
    # Exceptions
    "ScopeError",
    "ScopeConnectionError",
    "ScopeTimeoutError",
    "ScopeConfigurationError",
    "ScopeTriggerError",
    # Config dataclasses
    "ChannelConfig",
    "ChannelTrigger",
    "TriggerConfig",
    "AcquisitionConfig",
    "SequenceConfig",
    # Data dataclasses
    "WaveformData",
    "SequenceData",
    # Classes
    "TeledyneLecroyScope",
    "WP804HD",
    "WR8208HD",
]


class TriggerSlope(Enum):
    """Trigger edge direction (legacy, use TriggerState for pattern trigger)."""
    RISING = auto()
    FALLING = auto()
    EITHER = auto()


class TriggerState(Enum):
    """Per-channel trigger state for pattern trigger."""
    HIGH = "H"       # Trigger on high level (rising edge)
    LOW = "L"        # Trigger on low level (falling edge)
    DONT_CARE = "X"  # Don't use this channel for triggering


class Coupling(Enum):
    """Channel input coupling/termination."""
    DC50 = "D50"   # 50Ω DC
    DC1M = "D1M"   # 1MΩ DC
    AC1M = "A1M"   # 1MΩ AC
    GND = "GND"   # Ground


class AuxOutputMode(Enum):
    """Auxiliary output mode."""
    TRIGGER_OUT = "TriggerOut"           # Output trigger pulse
    TRIGGER_ENABLED = "TriggerEnabled"   # Output when trigger is armed/enabled


# Settings options metadata for JSON export
# Maps setting names to their valid string values
SETTINGS_OPTIONS = {
    "coupling": [e.name for e in Coupling],
    "trigger_state": [e.name for e in TriggerState],
    "trigger_mode": ["AUTO", "NORM", "SINGLE", "STOP"],
    "trigger_logic": ["OR", "AND"],
    "auxiliary_output": [e.name for e in AuxOutputMode],
    "switch": ["ON", "OFF"],
}


# === Exceptions ===

class ScopeError(Exception):
    """Base exception for Teledyne LeCroy scope errors."""
    pass


class ScopeConnectionError(ScopeError):
    """Connection failure (network, VISA, etc.)."""
    pass


class ScopeTimeoutError(ScopeError):
    """Response timeout."""
    pass


class ScopeConfigurationError(ScopeError):
    """Invalid configuration value."""
    pass


class ScopeTriggerError(ScopeError):
    """Trigger-related error (no signal, etc.)."""
    pass


# === Configuration Dataclasses ===

@dataclass
class ChannelConfig:
    """Single channel configuration."""
    vdiv: float = 0.020            # V/div
    offset: float = 0.0            # V
    coupling: Coupling = Coupling.DC50
    enabled: bool = True


@dataclass
class ChannelTrigger:
    """Per-channel trigger configuration.

    Each channel can be HIGH, LOW, or DONT_CARE (X).
    Level can be absolute or relative to baseline.
    """
    state: TriggerState = TriggerState.DONT_CARE
    level: float | None = None     # V absolute (if set, ignores level_offset)
    level_offset: float = 0.0      # V relative to baseline (used if level is None)


@dataclass
class TriggerConfig:
    """Trigger configuration with per-channel settings.

    Example:
        TriggerConfig(
            channels={
                1: ChannelTrigger(state=TriggerState.HIGH, level=0.1),
                2: ChannelTrigger(state=TriggerState.LOW, level=-0.05),
            },
            mode="SINGLE"
        )

    External trigger example:
        TriggerConfig(
            external=True,
            external_level=1.25,  # V
            channels={1: ChannelTrigger(state=TriggerState.HIGH, level=0.1)},
        )
    """
    channels: dict[int, ChannelTrigger] = field(default_factory=dict)
    mode: Literal["AUTO", "NORM", "SINGLE", "STOP"] = "SINGLE"
    logic: Literal["OR", "AND"] = "OR"
    external: bool = False         # Use external trigger (EX)
    external_level: float = 1.25   # External trigger level (V)


@dataclass
class AcquisitionConfig:
    """Acquisition timing configuration."""
    tdiv: float = 5e-9             # s/div
    sampling_period: float = 25e-12  # s (25ps = 40GS/s)
    trigger_delay: float = 0.0     # s
    window_delay: float = 10e-9    # s
    max_samples: int | None = None  # Max points per record/segment (optional cap)
    acquisition_mode: Literal["fixed_sample_rate", "set_maximum_memory"] = "fixed_sample_rate"


@dataclass
class SequenceConfig:
    """Sequence mode configuration."""
    enabled: bool = False
    num_segments: int = 1
    timeout_enabled: bool = False
    timeout_seconds: float = 2.5e6


# === Data Dataclasses ===

@dataclass(frozen=True)
class WaveformData:
    """Single segment waveform data (immutable)."""
    raw_data: bytes
    channel: int
    segment: int = 0               # Segment index (0-based)
    dx: float = 0.0                # Time step (s)
    x0: float = 0.0                # Time origin (s)
    dy: float = 0.0                # Voltage scale (V/ADC)
    y0: float = 0.0                # Voltage offset (V)
    trigger_time: str | None = None
    sample_width_bytes: Literal[1, 2] = 1
    byte_order: Literal["little", "big"] = "little"
    points: int | None = None

    def to_voltage(self) -> NDArray[np.float64]:
        """Convert raw bytes to voltage array."""
        if self.sample_width_bytes == 1:
            raw = np.frombuffer(self.raw_data, dtype=np.int8)
        elif self.sample_width_bytes == 2:
            dtype = "<i2" if self.byte_order == "little" else ">i2"
            raw = np.frombuffer(self.raw_data, dtype=dtype)
        else:
            raise ValueError(f"Unsupported sample width: {self.sample_width_bytes}")
        return raw * self.dy + self.y0

    def to_time(self) -> NDArray[np.float64]:
        """Generate time axis array."""
        if self.points is not None:
            n_points = self.points
        else:
            n_points = len(self.raw_data) // self.sample_width_bytes
        return np.arange(n_points) * self.dx + self.x0


@dataclass(frozen=True)
class SequenceData:
    """Sequence mode data container (immutable)."""
    segments: tuple[WaveformData, ...]
    channel: int

    def __len__(self) -> int:
        return len(self.segments)

    def __getitem__(self, idx: int) -> WaveformData:
        return self.segments[idx]

    def to_voltage_array(self) -> NDArray[np.float64]:
        """Convert all segments to 2D array (n_segments, n_points).
        
        Trims all segments to the minimum length to ensure a valid 2D array.
        """
        arrays = [seg.to_voltage() for seg in self.segments]
        if not arrays:
            return np.array([])
            
        # Ensure all segments have the same length (trim to min length)
        min_len = min(len(a) for a in arrays)
        if any(len(a) != min_len for a in arrays):
            arrays = [a[:min_len] for a in arrays]
            
        return np.array(arrays)


# === Base Class ===

class TeledyneLecroyScope(ABC):
    """Abstract base class for Teledyne LeCroy oscilloscopes."""

    # Hardware specs (override in subclasses)
    MAX_SAMPLING_RATE: float = 40e9   # samples/s
    MIN_MEMORY_SIZE: int = 500        # points
    MAX_MEMORY_SIZE: int | None = None
    MAX_SEQUENCE_MEMORY_SIZE: int | None = None
    MAX_MEMORY_SIZE_BY_ACTIVE_CHANNELS: dict[int, int] | None = None
    MAX_SEQUENCE_MEMORY_SIZE_BY_ACTIVE_CHANNELS: dict[int, int] | None = None
    MAX_SAMPLING_RATE_BY_ACTIVE_CHANNELS: dict[int, float] | None = None
    TIME_DIVISIONS: int = 10
    VOLTAGE_DIVISIONS: int = 8
    MAX_CHANNELS: int = 4
    DY_ADC_CONVERSION: float = 0.03125  # V/ADC = vdiv * 8 / 256
    X0_DIVISION: int = -5  # Time divisions for x0 calculation
    MAX_MEASUREMENT_PARAMS: int = 12
    MAX_MATH_TRACES: int = 8
    _VBS_FALLBACK_ALLOWED = {
        "channel_scale",
        "channel_offset",
        "channel_coupling",
        "channel_view",
        "acquisition_horizontal_scale",
        "acquisition_num_segments",
        "acquisition_sequence_timeout",
        "trigger_mode_readback",
        "trigger_source_readback",
        "trigger_level_readback",
        "aux_status_read",
    }
    _VBS_FALLBACK_FORBIDDEN = {
        "trigger_pattern_write",
        "sequence_arm_state_write",
    }

    def __init__(
        self,
        address: str,
        protocol: Literal["lxi", "vicp"] = "lxi",
        timeout: float = 30.0,
        active_channels: list[int] | None = None,
    ) -> None:
        self._address = address
        self._protocol = protocol.lower()
        if self._protocol not in ("lxi", "vicp"):
            raise ScopeConfigurationError(
                f"Invalid protocol: {protocol}. Use 'lxi' or 'vicp'."
            )
        self._timeout = timeout
        self._active_channels = active_channels or [1, 2, 3, 4]
        self._scope: MessageBasedResource | None = None
        self._rm: pyvisa.ResourceManager | None = None
        self._connected = False
        self._logger = logging.getLogger(self.__class__.__name__)
        self._vbs_logger = logging.getLogger("teledyne_lecroy.vbs")
        self._vbs_corr_id = count(1)

        # State tracking
        self._channel_configs: dict[int, ChannelConfig] = {}
        self._acquisition_config: AcquisitionConfig | None = None
        self._trigger_config: TriggerConfig | None = None
        self._sequence_config: SequenceConfig | None = None
        self._settings: dict = {}

        # Auto-save settings
        self._output_dir: Path | None = None
        self._auto_save_settings: bool = False
        self._last_sequence_profile: dict[str, Any] | None = None

        # Validate channels
        for ch in self._active_channels:
            if not 1 <= ch <= self.MAX_CHANNELS:
                raise ScopeConfigurationError(f"Invalid channel: {ch}")

    def __enter__(self) -> TeledyneLecroyScope:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disconnect()

    def connect(self) -> None:
        """Establish connection to scope."""
        if self._connected:
            return
        if pyvisa is None:
            raise ScopeConnectionError(
                "pyvisa is required to connect to the scope. "
                "Install with: pip install pyvisa pyvisa-py"
            )

        try:
            self._rm = pyvisa.ResourceManager()
            resource_name = self._build_resource_name()
            self._scope = self._rm.open_resource(
                resource_name,
                resource_pyclass=MessageBasedResource,
            )
            self._scope.timeout = int(self._timeout * 1000)
            self._connected = True

            # Suppress pyvisa logging
            logging.getLogger("pyvisa").setLevel(logging.WARNING)

            idn = self.query("*IDN?")
            self._logger.info(f"Connected: {idn}")

            # Load current settings from scope
            self._settings = self.read_all_settings()

        except Exception as e:
            if pyvisa is not None and isinstance(e, pyvisa.Error):
                raise ScopeConnectionError(
                    f"Connection failed ({self._protocol.upper()}): {self._address}"
                ) from e
            raise

    def _build_resource_name(self) -> str:
        """Build VISA resource name from selected transport protocol."""
        if self._protocol == "vicp":
            return f"VICP::{self._address}::INSTR"
        # LXI over VISA/TCPIP (existing behavior)
        return f"TCPIP0::{self._address}::inst0::INSTR"

    def disconnect(self) -> None:
        """Close connection to scope."""
        if self._scope:
            self._scope.close()
        if self._rm:
            self._rm.close()
        self._scope = None
        self._rm = None
        self._connected = False
        self._logger.info("Disconnected")

    def get_last_sequence_profile(self) -> dict[str, Any] | None:
        """Return latest sequence readout timing profile (if available)."""
        if self._last_sequence_profile is None:
            return None
        return dict(self._last_sequence_profile)

    # === Low-level Communication ===

    def _ensure_connected(self) -> None:
        """Raise if not connected."""
        if not self._connected or self._scope is None:
            raise ScopeConnectionError("Not connected to scope")

    def write(self, command: str) -> None:
        """Send SCPI command."""
        self._ensure_connected()
        self._scope.write(command)
        self._logger.debug(f"WRITE: {command}")

    def query(self, command: str) -> str:
        """Send SCPI query and return response."""
        self._ensure_connected()
        response = self._scope.query(command).strip()
        self._logger.debug(f"QUERY: {command} -> {response}")
        return response

    def _mask_log_value(self, value: str) -> str:
        """Mask sensitive values before writing diagnostic logs."""
        masked = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "<ip>", value)
        masked = re.sub(r"/[^\s'\"]+", "<path>", masked)
        return masked

    def _log_vbs_exchange(
        self,
        correlation_id: int,
        command: str,
        response: str,
        duration_ms: float,
    ) -> None:
        command_m = self._mask_log_value(command)
        response_m = self._mask_log_value(response)
        max_len = 512
        if len(response_m) > max_len:
            response_m = response_m[:max_len] + "...<truncated>"
        self._vbs_logger.debug(
            "cid=%s duration_ms=%.3f cmd=%s resp=%s",
            correlation_id,
            duration_ms,
            command_m,
            response_m,
        )

    def _vbs_write(
        self,
        expression: str,
        *,
        operation: str,
        fallback_scpi: str | None = None,
    ) -> None:
        """Execute a VBS write expression with policy-controlled fallback."""
        command = f"""vbs '{expression}' """
        correlation_id = next(self._vbs_corr_id)
        start = time.perf_counter()
        try:
            self.write(command)
            duration_ms = (time.perf_counter() - start) * 1000.0
            self._log_vbs_exchange(correlation_id, command, "OK", duration_ms)
            return
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000.0
            self._log_vbs_exchange(
                correlation_id,
                command,
                f"ERROR:{type(exc).__name__}:{exc}",
                duration_ms,
            )
            if operation in self._VBS_FALLBACK_FORBIDDEN:
                raise ScopeConfigurationError(
                    f"VBS operation '{operation}' failed and fallback is forbidden"
                ) from exc
            if fallback_scpi and operation in self._VBS_FALLBACK_ALLOWED:
                self.write(fallback_scpi)
                return
            raise

    def _vbs_query(
        self,
        expression: str,
        *,
        operation: str,
        fallback_scpi: str | None = None,
    ) -> str:
        """Execute a VBS query expression with policy-controlled fallback."""
        command = f"""vbs? 'return={expression}' """
        correlation_id = next(self._vbs_corr_id)
        start = time.perf_counter()
        try:
            response = self.query(command)
            duration_ms = (time.perf_counter() - start) * 1000.0
            self._log_vbs_exchange(correlation_id, command, response, duration_ms)
            return response
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000.0
            self._log_vbs_exchange(
                correlation_id,
                command,
                f"ERROR:{type(exc).__name__}:{exc}",
                duration_ms,
            )
            if operation in self._VBS_FALLBACK_FORBIDDEN:
                raise ScopeConfigurationError(
                    f"VBS operation '{operation}' failed and fallback is forbidden"
                ) from exc
            if fallback_scpi and operation in self._VBS_FALLBACK_ALLOWED:
                return self.query(fallback_scpi)
            raise

    def _normalize_run_state(self, raw: str) -> str:
        text = raw.strip().strip('"').upper()
        if "STOP" in text:
            return "STOP"
        if "SINGLE" in text or "AUTO" in text or "NORM" in text:
            return "RUN"
        if "RUN" in text:
            return "RUN"
        return text

    def _resolve_state_timeout(
        self,
        timeout: float | None,
        *,
        sequence_operation: bool,
    ) -> float:
        if timeout is not None:
            base = timeout
        else:
            seq_settings = self._settings.get("sequence", {})
            configured = seq_settings.get("timeout_seconds") if isinstance(seq_settings, dict) else None
            base = float(configured) if configured is not None else 10.0
        # RunState transitions should never wait for capture-timeout scale values.
        base = min(base, 30.0)
        if sequence_operation:
            return max(5.0, min(base + 2.0, 60.0))
        return max(5.0, base)

    def _read_run_state(self) -> str:
        # Older firmware can reject app.Acquisition.RunState VBS paths.
        # TRMD? is the stable cross-model readback.
        response_text = self.query("TRMD?")
        return self._normalize_run_state(response_text)

    def _poll_state_stable(
        self,
        target: str,
        *,
        timeout: float,
        interval_s: float = 0.05,
        stable_count: int = 3,
    ) -> bool:
        deadline = time.monotonic() + timeout
        consecutive = 0
        trace: list[str] = []
        while time.monotonic() < deadline:
            current = self._read_run_state()
            trace.append(current)
            if current == target:
                consecutive += 1
                if consecutive >= stable_count:
                    return True
            else:
                consecutive = 0
            time.sleep(interval_s)
        self._vbs_logger.warning(
            "runstate_transition_timeout target=%s trace=%s",
            target,
            ",".join(trace[-20:]),
        )
        return False

    def set_run_state(
        self,
        target: Literal["AUTO", "NORM", "SINGLE", "STOP", "RUN"],
        *,
        timeout: float | None = None,
        sequence_operation: bool = False,
        verify_transition: bool = True,
    ) -> None:
        """Set run state using SCPI and stable readback polling."""
        if target == "STOP":
            self.write("TRMD STOP")
        elif target == "RUN":
            # SCPI has no explicit RUN token; NORM keeps acquisition running.
            self.write("TRMD NORM")
        else:
            self.write(f"TRMD {target}")

        if not verify_transition:
            return

        normalized = self._normalize_run_state(target)
        resolved_timeout = self._resolve_state_timeout(
            timeout,
            sequence_operation=sequence_operation,
        )
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                if self._poll_state_stable(normalized, timeout=resolved_timeout):
                    return
                last_error = ScopeTimeoutError(
                    f"RunState transition to {target} timed out after {resolved_timeout:.3f}s"
                )
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise ScopeTimeoutError(f"RunState transition to {target} failed")

    def read_raw(self) -> bytes:
        """Read raw binary data."""
        self._ensure_connected()
        return self._scope.read_raw()

    def wait_opc(self, timeout: float | None = None) -> None:
        """Wait for operation complete (*OPC?)."""
        self._ensure_connected()
        old_timeout = self._scope.timeout
        if timeout:
            self._scope.timeout = int(timeout * 1000)
        try:
            while True:
                opc = self.query("*OPC?").strip()
                if opc == "1":
                    break
                time.sleep(0.01)
        finally:
            self._scope.timeout = old_timeout

    def clear(self) -> None:
        """Clear scope registers and buffers."""
        self._ensure_connected()
        self._scope.clear()
        self._logger.info("Scope cleared")

    def clear_sweeps(self) -> None:
        """Clear all accumulated sweeps and waveform data."""
        self._ensure_connected()
        self.write("CLSW")
        self._logger.info("Sweeps cleared")

    def clear_all_memory(self) -> None:
        """Clear all internal memory including reference waveforms and zoom traces."""
        self._ensure_connected()
        self._vbs_write(
            "app.Memory.ClearAllMem",
            operation="memory_clear_all"
        )
        self._logger.info("All memory cleared")

    @property
    def settings(self) -> dict:
        """Current settings state (read-only copy)."""
        return self._settings.copy()

    def _update_settings(self, section: str, key: str | None = None, value: Any = None, data: dict | None = None) -> None:
        """Update internal settings state.

        Args:
            section: Top-level section (channels, acquisition, trigger, sequence, auxiliary_output)
            key: Sub-key within section (optional)
            value: Value to set (used with key)
            data: Dict to merge into section (used without key)
        """
        if section not in self._settings:
            self._settings[section] = {}

        if data is not None:
            if isinstance(self._settings[section], dict):
                self._settings[section].update(data)
            else:
                self._settings[section] = data
        elif key is not None:
            self._settings[section][key] = value
        else:
            self._settings[section] = value

    def print_settings(self) -> None:
        """Print current settings in a readable format."""
        import json
        print(json.dumps(self._settings, indent=2))

    # === Configuration ===

    def configure(
        self,
        channels: dict[int, ChannelConfig] | None = None,
        acquisition: AcquisitionConfig | None = None,
        sequence: SequenceConfig | None = None,
        display: bool | None = None,
    ) -> None:
        """Configure scope with given settings.

        All parameters are optional - only provided settings will be updated.

        Args:
            channels: Channel configurations (partial update supported)
            acquisition: Acquisition/timebase configuration
            sequence: Sequence mode configuration
            display: Keep display on (True) or off for faster acquisition (False)
        """
        self._ensure_connected()
        self.write("CHDR OFF")  # Remove response headers

        # Configure display if specified
        if display is not None:
            self._configure_display(display=display)

        # Configure acquisition/timebase if provided
        if acquisition is not None:
            effective_sequence = sequence if sequence is not None else self._sequence_config
            resolved_acquisition = self._normalize_acquisition(
                acquisition,
                channels=channels,
                sequence=effective_sequence,
            )
            self._configure_timebase(resolved_acquisition)
            self._acquisition_config = resolved_acquisition
            self._settings["acquisition"] = {
                "tdiv": resolved_acquisition.tdiv,
                "sampling_period": resolved_acquisition.sampling_period,
                "trigger_delay": resolved_acquisition.trigger_delay,
                "window_delay": resolved_acquisition.window_delay,
                "max_samples": resolved_acquisition.max_samples,
                "acquisition_mode": resolved_acquisition.acquisition_mode,
            }

        # Configure channels if provided
        if channels is not None:
            self._validate_channels(channels)
            for ch, cfg in channels.items():
                self._configure_channel(ch, cfg)
                # Update internal state
                if self._channel_configs is None:
                    self._channel_configs = {}
                self._channel_configs[ch] = cfg
                # Update settings state
                if "channels" not in self._settings:
                    self._settings["channels"] = {}
                self._settings["channels"][str(ch)] = {
                    "vdiv": cfg.vdiv,
                    "offset": cfg.offset,
                    "coupling": cfg.coupling.name,
                    "enabled": cfg.enabled,
                }

        # Configure sequence if provided
        if sequence is not None:
            if sequence.enabled:
                self._configure_sequence(sequence)
            else:
                # Explicitly turn off sequence mode
                self.write("SEQ OFF")
                self._logger.info("Sequence mode disabled")
            self._sequence_config = sequence
            self._settings["sequence"] = {
                "enabled": sequence.enabled,
                "num_segments": sequence.num_segments,
                "timeout_enabled": sequence.timeout_enabled,
                "timeout_seconds": sequence.timeout_seconds,
            }

        self._logger.info("Configuration complete")

    def _validate_channels(self, channels: dict[int, ChannelConfig]) -> None:
        """Validate channel configuration values."""
        for ch in channels:
            if not 1 <= ch <= self.MAX_CHANNELS:
                raise ScopeConfigurationError(f"Invalid channel: {ch}")

    def _validate_acquisition(self, acquisition: AcquisitionConfig) -> None:
        """Validate acquisition configuration values."""
        max_window = self.TIME_DIVISIONS / 2 * acquisition.tdiv
        if acquisition.window_delay > max_window:
            raise ScopeConfigurationError(
                f"window_delay ({acquisition.window_delay}s) exceeds max ({max_window}s)"
            )

    def _get_enabled_channel_count(
        self, channels: dict[int, ChannelConfig] | None = None
    ) -> int:
        """Estimate active/visible channel count after a pending channel update."""
        enabled: dict[int, bool] = {ch: True for ch in self._active_channels}
        for ch, cfg in self._channel_configs.items():
            if ch in enabled:
                enabled[ch] = bool(cfg.enabled)
        if channels:
            for ch, cfg in channels.items():
                if ch in enabled:
                    enabled[ch] = bool(cfg.enabled)
        count_enabled = sum(1 for on in enabled.values() if on)
        return max(1, count_enabled)

    def _get_max_sampling_rate_for_enabled_channels(
        self, enabled_channels: int
    ) -> float:
        """Resolve max sample rate from model defaults and channel count map."""
        limits = self.MAX_SAMPLING_RATE_BY_ACTIVE_CHANNELS
        if not limits:
            return self.MAX_SAMPLING_RATE
        if enabled_channels in limits:
            return limits[enabled_channels]
        sorted_keys = sorted(limits)
        for key in reversed(sorted_keys):
            if key <= enabled_channels:
                return limits[key]
        return limits[sorted_keys[0]]

    @staticmethod
    def _resolve_limit_by_enabled_channels(
        enabled_channels: int,
        *,
        default_limit: int | None,
        per_channel_limits: dict[int, int] | None,
    ) -> int | None:
        """Resolve a size limit using per-channel map when available."""
        if per_channel_limits:
            if enabled_channels in per_channel_limits:
                return per_channel_limits[enabled_channels]
            sorted_keys = sorted(per_channel_limits)
            for key in reversed(sorted_keys):
                if key <= enabled_channels:
                    return per_channel_limits[key]
            return per_channel_limits[sorted_keys[0]]
        return default_limit

    def _get_max_points_per_record(
        self,
        sequence: SequenceConfig | None,
        *,
        enabled_channels: int,
    ) -> float | None:
        """Return max allowed points per record/segment, if model limits are known."""
        if sequence and sequence.enabled:
            max_samples_total = self._resolve_limit_by_enabled_channels(
                enabled_channels,
                default_limit=self.MAX_SEQUENCE_MEMORY_SIZE,
                per_channel_limits=self.MAX_SEQUENCE_MEMORY_SIZE_BY_ACTIVE_CHANNELS,
            )
        else:
            max_samples_total = self._resolve_limit_by_enabled_channels(
                enabled_channels,
                default_limit=self.MAX_MEMORY_SIZE,
                per_channel_limits=self.MAX_MEMORY_SIZE_BY_ACTIVE_CHANNELS,
            )
        if max_samples_total is None:
            return None
        if sequence and sequence.enabled:
            segments = max(1, int(sequence.num_segments))
            return float(max_samples_total) / float(segments)
        return float(max_samples_total)

    def _normalize_acquisition(
        self,
        acquisition: AcquisitionConfig,
        *,
        channels: dict[int, ChannelConfig] | None,
        sequence: SequenceConfig | None,
    ) -> AcquisitionConfig:
        """Normalize acquisition values against model/channel/sequence limits."""
        if acquisition.tdiv <= 0:
            raise ScopeConfigurationError(f"tdiv must be > 0, got {acquisition.tdiv}")
        if acquisition.sampling_period <= 0:
            raise ScopeConfigurationError(
                f"sampling_period must be > 0, got {acquisition.sampling_period}"
            )
        if acquisition.acquisition_mode not in {"fixed_sample_rate", "set_maximum_memory"}:
            raise ScopeConfigurationError(
                f"Invalid acquisition_mode: {acquisition.acquisition_mode}"
            )

        resolved_tdiv = acquisition.tdiv
        resolved_sampling_period = acquisition.sampling_period
        enabled_channels = self._get_enabled_channel_count(channels)
        max_sr = self._get_max_sampling_rate_for_enabled_channels(enabled_channels)
        if acquisition.acquisition_mode == "fixed_sample_rate":
            requested_sr = 1.0 / resolved_sampling_period
            if requested_sr > max_sr:
                self._logger.warning(
                    "Requested sample rate %.3e Sa/s exceeds max %.3e Sa/s for %d active channels; clamping.",
                    requested_sr,
                    max_sr,
                    enabled_channels,
                )
                resolved_sampling_period = 1.0 / max_sr

        max_points = self._get_max_points_per_record(
            sequence,
            enabled_channels=enabled_channels,
        )
        effective_max_points = max_points
        requested_cap = acquisition.max_samples
        resolved_cap: int | None = None
        if requested_cap is not None:
            if requested_cap <= 0:
                raise ScopeConfigurationError(
                    f"max_samples must be > 0, got {requested_cap}"
                )
            resolved_cap = max(self.MIN_MEMORY_SIZE, int(requested_cap))
            if resolved_cap != requested_cap:
                self._logger.warning(
                    "Requested max_samples %d is below MIN_MEMORY_SIZE %d; clamping.",
                    requested_cap,
                    self.MIN_MEMORY_SIZE,
                )
            if effective_max_points is None:
                effective_max_points = float(resolved_cap)
            else:
                if resolved_cap > int(effective_max_points):
                    self._logger.warning(
                        "Requested max_samples %d exceeds model/channel limit %.0f; using %.0f.",
                        resolved_cap,
                        effective_max_points,
                        effective_max_points,
                    )
                effective_max_points = min(effective_max_points, float(resolved_cap))
            if effective_max_points is not None:
                resolved_cap = int(effective_max_points)

        if effective_max_points is not None:
            requested_points = (
                self.TIME_DIVISIONS * resolved_tdiv / resolved_sampling_period
            )
            if requested_points > effective_max_points:
                adjusted_sampling_period = (
                    self.TIME_DIVISIONS * resolved_tdiv / effective_max_points
                )
                self._logger.warning(
                    "Requested TDIV %.3e s / sampling period %.3e s requires %.0f points but max is %.0f; clamping sampling period to %.3e s.",
                    resolved_tdiv,
                    resolved_sampling_period,
                    requested_points,
                    effective_max_points,
                    adjusted_sampling_period,
                )
                resolved_sampling_period = adjusted_sampling_period

        requested_points = self.TIME_DIVISIONS * resolved_tdiv / resolved_sampling_period
        if requested_points < 1.0:
            adjusted_tdiv = resolved_sampling_period / self.TIME_DIVISIONS
            self._logger.warning(
                "Requested TDIV %.3e s yields less than one sample (%.3f); clamping TDIV to %.3e s.",
                resolved_tdiv,
                requested_points,
                adjusted_tdiv,
            )
            resolved_tdiv = adjusted_tdiv

        resolved_window_delay = acquisition.window_delay
        max_window = self.TIME_DIVISIONS / 2 * resolved_tdiv
        if resolved_window_delay > max_window:
            self._logger.warning(
                "Requested window_delay %.3e s exceeds max %.3e s for resolved TDIV; clamping.",
                resolved_window_delay,
                max_window,
            )
            resolved_window_delay = max_window

        resolved = AcquisitionConfig(
            tdiv=resolved_tdiv,
            sampling_period=resolved_sampling_period,
            trigger_delay=acquisition.trigger_delay,
            window_delay=resolved_window_delay,
            max_samples=resolved_cap,
            acquisition_mode=acquisition.acquisition_mode,
        )
        self._validate_acquisition(resolved)
        return resolved

    def _calculate_memory_size(self, acquisition: AcquisitionConfig) -> float:
        """Calculate memory size based on sampling rate."""
        return (
            self.TIME_DIVISIONS * acquisition.tdiv / acquisition.sampling_period
        )

    def _calculate_num_points(self, acquisition: AcquisitionConfig) -> int:
        """Calculate number of acquisition points."""
        return int(
            self.TIME_DIVISIONS * acquisition.tdiv / acquisition.sampling_period
        )

    # === Read Settings from Scope ===

    def _parse_numeric_response(self, response: str) -> float:
        """Parse numeric value from SCPI response.

        Handles formats like:
        - "5E-6" (value only)
        - "TDIV 5E-6 S" (command + value + unit)
        - "C1:VDIV 2E-1 V" (channel:command + value + unit)
        """
        # Try to find a numeric value in the response
        for part in response.split():
            # Skip parts that look like commands or units
            if ":" in part or part.isalpha():
                continue
            try:
                return float(part)
            except ValueError:
                continue

        # If no numeric found, try the whole string (minus trailing unit)
        parts = response.split()
        if len(parts) >= 2:
            try:
                return float(parts[-2])  # Value is typically second-to-last
            except ValueError:
                pass

        raise ValueError(f"Could not parse numeric value from: {response}")

    def _parse_wavedesc_metadata(
        self, inspect_response: str
    ) -> tuple[int, Literal[1, 2], Literal["little", "big"]]:
        """Parse key WAVEDESC values from INSPECT response text."""
        points = 0
        points_match = re.search(r"WAVE_ARRAY_COUNT\s*:\s*(\d+)", inspect_response)
        if points_match:
            points = int(points_match.group(1))

        sample_width: Literal[1, 2] = 1
        comm_type_match = re.search(r"COMM_TYPE\s*:\s*([A-Z0-9_]+)", inspect_response.upper())
        if comm_type_match:
            comm_type = comm_type_match.group(1)
            if comm_type in {"WORD", "2"}:
                sample_width = 2

        byte_order: Literal["little", "big"] = "little"
        comm_order_match = re.search(r"COMM_ORDER\s*:\s*([A-Z0-9_]+)", inspect_response.upper())
        if comm_order_match:
            comm_order = comm_order_match.group(1)
            if comm_order in {"HIFIRST", "HI_FIRST", "BIG", "BIGENDIAN", "MSB"}:
                byte_order = "big"
            elif comm_order in {"LOFIRST", "LO_FIRST", "LITTLE", "LITTLEENDIAN", "LSB"}:
                byte_order = "little"

        return points, sample_width, byte_order

    def read_channel_config(self, channel: int) -> ChannelConfig:
        """Read current channel configuration from scope."""
        self._ensure_connected()
        if not 1 <= channel <= self.MAX_CHANNELS:
            raise ScopeConfigurationError(f"Invalid channel: {channel}")

        vdiv = self._parse_numeric_response(self.query(f"C{channel}:VDIV?"))
        offset = self._parse_numeric_response(self.query(f"C{channel}:OFST?"))
        trace_state = self.query(f"C{channel}:TRA?")
        enabled = "ON" in trace_state.upper()

        return ChannelConfig(vdiv=vdiv, offset=offset, enabled=enabled)

    def read_acquisition_config(self) -> AcquisitionConfig:
        """Read current acquisition configuration from scope."""
        self._ensure_connected()

        tdiv = self._parse_numeric_response(self.query("TDIV?"))

        # Calculate sampling period from current memory size
        memory_response = self.query("MSIZ?")
        # Parse memory size (e.g., "10000", "10K", "1M", or "MSIZ 10000")
        memory_str = memory_response.upper()
        # Remove command prefix if present
        if "MSIZ" in memory_str:
            memory_str = memory_str.replace("MSIZ", "").strip()
        # Handle K/M suffixes
        memory_str = memory_str.replace("K", "E3").replace("M", "E6").replace("SA", "").strip()
        try:
            memory_size = self._parse_numeric_response(memory_str)
            sampling_period = self.TIME_DIVISIONS * tdiv / memory_size
        except ValueError:
            # Fallback to a reasonable default
            self._logger.warning(f"Could not parse memory size: {memory_response}")
            sampling_period = tdiv / 1000

        return AcquisitionConfig(tdiv=tdiv, sampling_period=sampling_period)

    def read_trigger_level(self, channel: int) -> float:
        """Read current trigger level for a channel."""
        self._ensure_connected()
        return self._parse_numeric_response(self.query(f"C{channel}:TRLV?"))

    def read_trigger_pattern(self) -> dict[int, str]:
        """Read current trigger pattern states for all channels.

        Returns:
            Dict mapping channel number to state ("H", "L", or "X")
        """
        self._ensure_connected()
        response = self.query("TRPA?")
        self._logger.debug(f"Trigger pattern response: {response}")

        # Parse response like "TRPA C1,H,C2,L,C3,X,C4,X,STATE,OR"
        states = {}
        parts = response.replace("TRPA", "").strip().split(",")
        for i in range(0, len(parts) - 2, 2):  # Skip last "STATE,OR" part
            ch_part = parts[i].strip()
            if ch_part.startswith("C") and len(parts) > i + 1:
                try:
                    ch = int(ch_part[1:])
                    state = parts[i + 1].strip().upper()
                    if state in ("H", "L", "X"):
                        states[ch] = state
                except (ValueError, IndexError):
                    pass

        return states

    @staticmethod
    def _parse_bool_response(value: str) -> bool:
        token = value.strip().strip('"').upper()
        if token in {"-1", "1", "TRUE", "ON"}:
            return True
        if token in {"0", "FALSE", "OFF"}:
            return False
        return "TRUE" in token or "ON" in token

    def read_measurement_math_visibility(
        self,
        *,
        measurement_slots: int | None = None,
        math_traces: int | None = None,
    ) -> dict[str, dict[int, bool]]:
        """Read remote visibility state for measurement/math traces via VBS."""
        self._ensure_connected()

        max_meas = measurement_slots or self.MAX_MEASUREMENT_PARAMS
        max_math = math_traces or self.MAX_MATH_TRACES
        measurements: dict[int, bool] = {}
        math: dict[int, bool] = {}

        for p_idx in range(1, max_meas + 1):
            try:
                response = self.query(
                    f"""vbs? 'return=app.Measure.P{p_idx}.View' """
                )
                measurements[p_idx] = self._parse_bool_response(response)
            except Exception as exc:
                self._logger.debug(
                    "Could not read measurement visibility P%s: %s",
                    p_idx,
                    exc,
                )

        for f_idx in range(1, max_math + 1):
            try:
                response = self.query(
                    f"""vbs? 'return=app.Math.F{f_idx}.View' """
                )
                math[f_idx] = self._parse_bool_response(response)
            except Exception as exc:
                self._logger.debug(
                    "Could not read math visibility F%s: %s",
                    f_idx,
                    exc,
                )

        return {"measurement": measurements, "math": math}

    def read_all_settings(self) -> dict:
        """Read all current settings from scope as a dictionary."""
        self._ensure_connected()

        # Read instrument-level settings
        instrument = {
            "display": "OFF",
            "grid": "QUATTRO",
            "bandwidth_limit": "OFF",
        }
        try:
            display_response = self.query("DISP?").strip().upper()
            if "ON" in display_response:
                instrument["display"] = "ON"
        except Exception as e:
            self._logger.debug(f"Could not read display state: {e}")
        try:
            grid_response = self.query("GRID?").strip()
            if grid_response:
                instrument["grid"] = grid_response.split()[-1].upper()
        except Exception as e:
            self._logger.debug(f"Could not read grid mode: {e}")
        try:
            bwl_response = self.query("BWL?").strip().upper()
            if "ON" in bwl_response:
                instrument["bandwidth_limit"] = "ON"
        except Exception as e:
            self._logger.debug(f"Could not read bandwidth limit: {e}")

        # Read ALL channel settings (not just enabled)
        channels = {}
        for ch in range(1, self.MAX_CHANNELS + 1):
            try:
                config = self.read_channel_config(ch)

                # Read coupling
                coupling_str = "DC50"  # default
                try:
                    coupling_response = self.query(f"C{ch}:CPL?").strip()
                    # Parse coupling (e.g., "C1:CPL D50" or "D50")
                    coupling_value = coupling_response.split()[-1]
                    # Map to Coupling enum names
                    coupling_map = {
                        "D50": "DC50",
                        "D1M": "DC1M",
                        "A1M": "AC1M",
                        "GND": "GND",
                    }
                    coupling_str = coupling_map.get(coupling_value, "DC50")
                except Exception as e:
                    self._logger.debug(f"Could not read coupling for channel {ch}: {e}")

                channels[str(ch)] = {
                    "vdiv": config.vdiv,
                    "offset": config.offset,
                    "coupling": coupling_str,
                    "enabled": config.enabled,
                }
                try:
                    channels[str(ch)]["attenuation"] = self._parse_numeric_response(
                        self.query(f"C{ch}:ATTN?")
                    )
                except Exception as e:
                    self._logger.debug(f"Could not read attenuation for channel {ch}: {e}")
            except Exception as e:
                self._logger.debug(f"Could not read channel {ch}: {e}")

        # Read acquisition settings
        acq = self.read_acquisition_config()

        # Read trigger delay
        trigger_delay = 0.0
        try:
            trigger_delay = self._parse_numeric_response(self.query("TRDL?"))
        except Exception as e:
            self._logger.debug(f"Could not read trigger delay: {e}")

        # Read window delay (from waveform setup first point)
        window_delay = 10e-9  # default
        try:
            # Try to get from stored config if available
            if self._acquisition_config:
                window_delay = self._acquisition_config.window_delay
        except Exception as e:
            self._logger.debug(f"Could not read window delay: {e}")

        acquisition = {
            "tdiv": acq.tdiv,
            "sampling_period": acq.sampling_period,
            "trigger_delay": trigger_delay,
            "window_delay": window_delay,
            "max_samples": self._acquisition_config.max_samples if self._acquisition_config else None,
            "acquisition_mode": self._acquisition_config.acquisition_mode if self._acquisition_config else "fixed_sample_rate",
        }
        try:
            memory_response = self.query("MSIZ?")
            acquisition["memory_size"] = int(self._parse_numeric_response(memory_response))
        except Exception as e:
            self._logger.debug(f"Could not read memory size: {e}")
        try:
            sample_rate_response = self.query(
                r"""vbs? 'return=app.Acquisition.Horizontal.SampleRate' """
            )
            acquisition["sample_rate"] = self._parse_numeric_response(sample_rate_response)
        except Exception as e:
            self._logger.debug(f"Could not read sample rate: {e}")

        # Read trigger mode (prefer VBS TriggerMode, fallback to TRMD?)
        trigger_mode = "SINGLE"
        try:
            trigger_response = self.query(
                r"""vbs? 'return=app.Acquisition.TriggerMode' """
            ).strip().strip('"')
            trig_u = trigger_response.upper()
            if "AUTO" in trig_u:
                trigger_mode = "AUTO"
            elif "NORM" in trig_u:
                trigger_mode = "NORM"
            elif "SINGLE" in trig_u:
                trigger_mode = "SINGLE"
            elif "STOP" in trig_u:
                trigger_mode = "STOP"
            elif trigger_response:
                trigger_mode = trigger_response.split()[-1].upper()
        except Exception:
            trigger_response = self.query("TRMD?").strip()
            trigger_mode = trigger_response.split()[-1].upper() if trigger_response else "SINGLE"

        # Read trigger pattern states
        trpa_response = ""
        try:
            trpa_response = self.query("TRPA?")
            pattern_states = self.read_trigger_pattern()
        except Exception as e:
            self._logger.debug(f"Could not read trigger pattern: {e}")
            pattern_states = {}

        trigger_logic = "OR"
        try:
            parts = [part.strip().upper() for part in trpa_response.split(",") if part.strip()]
            if "STATE" in parts:
                idx = parts.index("STATE")
                if idx + 1 < len(parts) and parts[idx + 1] in ("OR", "AND"):
                    trigger_logic = parts[idx + 1]
        except Exception as e:
            self._logger.debug(f"Could not parse trigger logic: {e}")

        # Build per-channel trigger config
        trigger_channels = {}
        state_map = {"H": "HIGH", "L": "LOW", "X": "DONT_CARE"}

        for ch in range(1, self.MAX_CHANNELS + 1):
            try:
                level = self.read_trigger_level(ch)
                state_code = pattern_states.get(ch, "X")
                state_name = state_map.get(state_code, "DONT_CARE")

                trigger_channels[str(ch)] = {
                    "state": state_name,
                    "level": level,
                }
            except Exception as e:
                self._logger.debug(f"Could not read trigger for channel {ch}: {e}")

        # Read external trigger settings
        external = False
        external_level = 1.25  # default
        try:
            # Check if external trigger is active in pattern
            trpa_response = self.query("TRPA?")
            # Parse for "EX,H" or "EX,L" (not "EX,X")
            if "EX,H" in trpa_response or "EX,L" in trpa_response:
                external = True

            # Read external trigger level
            external_level = self._parse_numeric_response(self.query("EX:TRLV?"))
        except Exception as e:
            self._logger.debug(f"Could not read external trigger settings: {e}")

        trigger = {
            "channels": trigger_channels,
            "mode": trigger_mode,
            "logic": trigger_logic,
            "external": external,
            "external_level": external_level,
        }

        # Read sequence configuration
        sequence = {
            "enabled": False,
            "num_segments": 1,
            "timeout_enabled": False,
            "timeout_seconds": 2.5e6,
        }
        try:
            seq_response = self.query("SEQ?").strip()
            # Parse response like "SEQ ON,100,2.5E+6" or "SEQ OFF"
            if "ON" in seq_response:
                sequence["enabled"] = True
                payload = seq_response
                if payload.upper().startswith("SEQ"):
                    payload = payload[3:].strip()
                if payload.upper().startswith("ON"):
                    payload = payload[2:].strip()
                if payload.startswith(","):
                    payload = payload[1:].strip()
                parts = [p.strip() for p in payload.split(",") if p.strip()]
                if len(parts) >= 1:
                    try:
                        sequence["num_segments"] = int(parts[0].strip())
                    except ValueError:
                        pass
                if len(parts) >= 2:
                    try:
                        sequence["timeout_seconds"] = float(parts[1].strip())
                    except ValueError:
                        pass

            # Try to read timeout enabled status via VBS
            try:
                timeout_response = self.query(
                    r"""vbs? 'return=app.Acquisition.Horizontal.SequenceTimeoutEnable' """
                )
                timeout_value = timeout_response.strip().strip('"').upper()
                sequence["timeout_enabled"] = (
                    "-1" in timeout_value
                    or timeout_value in {"1", "TRUE", "ON"}
                    or "TRUE" in timeout_value
                )
            except Exception:
                pass
        except Exception as e:
            self._logger.debug(f"Could not read sequence settings: {e}")

        # Read auxiliary output mode
        auxiliary_output = "TRIGGER_OUT"  # default
        try:
            try:
                aux_response = self.query(
                    r"""vbs? 'return=app.Acquisition.AuxOutput.AuxMode' """
                ).strip()
            except Exception:
                aux_response = self.query(
                    r"""vbs? 'return=app.Acquisition.AuxOutput.Mode' """
                ).strip()
            # Parse response and map to AuxOutputMode enum names
            aux_norm = aux_response.strip().strip('"').upper()
            if "TRIGGERENABLED" in aux_norm or aux_norm in {"1", "TRUE", "ON"}:
                auxiliary_output = "TRIGGER_ENABLED"
            elif "TRIGGEROUT" in aux_norm or aux_norm in {"0", "FALSE", "OFF"}:
                auxiliary_output = "TRIGGER_OUT"
        except Exception as e:
            self._logger.debug(f"Could not read auxiliary output: {e}")

        return {
            "_options": SETTINGS_OPTIONS,
            "instrument": instrument,
            "channels": channels,
            "acquisition": acquisition,
            "trigger": trigger,
            "sequence": sequence,
            "auxiliary_output": auxiliary_output,
        }

    def save_settings(self, filepath: str | Path) -> None:
        """Save current scope settings to JSON file."""
        filepath = Path(filepath)
        with filepath.open("w") as f:
            json.dump(self._settings, f, indent=2)
        self._logger.info(f"Settings saved to {filepath}")

    @staticmethod
    def load_settings_file(filepath: str | Path) -> dict:
        """Load settings from JSON file (static method, no scope needed)."""
        filepath = Path(filepath)
        with filepath.open() as f:
            return json.load(f)

    def apply_settings(self, settings: dict) -> None:
        """Apply settings from a dictionary (e.g., loaded from JSON).

        Converts dict format to typed dataclasses and calls configure().
        This ensures WFSU parameters are set correctly for the display range.

        Supports partial configs: only sections present in the dict are applied.
        Missing sections are left unchanged on the scope.

        Args:
            settings: Settings dict (from read_all_settings or load_settings_file)
        """
        self._ensure_connected()

        # Build channel configs only if "channels" section is present
        channels: dict[int, ChannelConfig] | None = None
        if "channels" in settings:
            channels = {}
            for ch_str, ch_data in settings["channels"].items():
                ch_num = int(ch_str)
                try:
                    current_cfg = self.read_channel_config(ch_num)
                except Exception:
                    current_cfg = ChannelConfig()

                coupling_default = "DC50"
                try:
                    coupling_response = self.query(f"C{ch_num}:CPL?").strip()
                    coupling_value = coupling_response.split()[-1]
                    coupling_default = {
                        "D50": "DC50",
                        "D1M": "DC1M",
                        "A1M": "AC1M",
                        "GND": "GND",
                    }.get(coupling_value, "DC50")
                except Exception:
                    pass

                coupling_name = str(ch_data.get("coupling", "DC50")).upper()
                if coupling_name not in Coupling.__members__:
                    self._logger.warning(
                        f"Unknown coupling '{coupling_name}' for channel {ch_str}; using DC50"
                    )
                    coupling_name = "DC50"
                if "coupling" not in ch_data:
                    coupling_name = coupling_default
                coupling = Coupling[coupling_name]
                channels[ch_num] = ChannelConfig(
                    vdiv=ch_data.get("vdiv", current_cfg.vdiv),
                    offset=ch_data.get("offset", current_cfg.offset),
                    coupling=coupling,
                    enabled=bool(ch_data.get("enabled", current_cfg.enabled)),
                )

        # Build acquisition config only if "acquisition" section is present
        acquisition: AcquisitionConfig | None = None
        if "acquisition" in settings:
            acq_data = settings["acquisition"]
            typed_keys = {"tdiv", "sampling_period", "trigger_delay", "window_delay", "max_samples", "acquisition_mode"}
            if any(k in acq_data for k in typed_keys):
                try:
                    current_acq = self.read_acquisition_config()
                except Exception:
                    current_acq = AcquisitionConfig()
                acquisition = AcquisitionConfig(
                    tdiv=acq_data.get("tdiv", current_acq.tdiv),
                    sampling_period=acq_data.get("sampling_period", current_acq.sampling_period),
                    trigger_delay=acq_data.get("trigger_delay", current_acq.trigger_delay),
                    window_delay=acq_data.get("window_delay", current_acq.window_delay),
                    max_samples=acq_data.get("max_samples", current_acq.max_samples),
                    acquisition_mode=acq_data.get("acquisition_mode", current_acq.acquisition_mode),
                )

        # Build sequence config only if "sequence" section is present
        sequence: SequenceConfig | None = None
        if "sequence" in settings:
            seq_data = settings["sequence"]
            sequence = SequenceConfig(
                enabled=seq_data.get("enabled", False),
                num_segments=seq_data.get("num_segments", 1),
                timeout_enabled=seq_data.get("timeout_enabled", False),
                timeout_seconds=seq_data.get("timeout_seconds", 2.5e6),
            )

        # Apply configuration if any section is present
        if channels is not None or acquisition is not None or sequence is not None:
            self.configure(
                channels=channels,
                acquisition=acquisition,
                sequence=sequence,
            )
        if "acquisition" in settings:
            acq_data = settings["acquisition"]
            if "memory_size" in acq_data:
                self.write(f"MSIZ {int(acq_data['memory_size'])}")
            if "sample_rate" in acq_data:
                self.write(
                    fr"""vbs 'app.Acquisition.Horizontal.SampleRate = {float(acq_data['sample_rate'])}' """
                )

        # Apply trigger settings if present
        if "trigger" in settings:
            trigger_data = settings["trigger"]
            current_trigger = self._settings.get("trigger", {}) if isinstance(self._settings, dict) else {}
            trigger_channels: dict[int, ChannelTrigger] = {}
            current_logic = str(current_trigger.get("logic", "OR")).upper()
            logic = str(trigger_data.get("logic", current_logic)).upper()
            if logic not in ("OR", "AND"):
                self._logger.warning(f"Unknown trigger logic '{logic}'; using OR")
                logic = "OR"
            for ch_str, tr_data in trigger_data.get("channels", {}).items():
                state = TriggerState[tr_data.get("state", "DONT_CARE")]
                trigger_channels[int(ch_str)] = ChannelTrigger(
                    state=state,
                    level=tr_data.get("level", 0.0),
                )

            trigger_config = TriggerConfig(
                channels=trigger_channels,
                mode=str(trigger_data.get("mode", current_trigger.get("mode", "NORM"))).upper(),
                logic=logic,
                external=bool(trigger_data.get("external", current_trigger.get("external", False))),
                external_level=float(trigger_data.get("external_level", current_trigger.get("external_level", 1.25))),
            )
            self.set_trigger(trigger_config)

        # Apply auxiliary output if present
        if "auxiliary_output" in settings:
            aux_mode = AuxOutputMode[settings["auxiliary_output"]]
            self.set_auxiliary_output(aux_mode)

        # Apply instrument-level settings if present
        if "instrument" in settings:
            instrument_data = settings["instrument"]
            if "display" in instrument_data:
                display_value = str(instrument_data["display"]).upper()
                self.write(f"DISP {'ON' if display_value in ('ON', 'TRUE', '1') else 'OFF'}")
            if "grid" in instrument_data:
                self.write(f"GRID {str(instrument_data['grid']).upper()}")
            if "bandwidth_limit" in instrument_data:
                bwl_value = str(instrument_data["bandwidth_limit"]).upper()
                self.write(f"BWL {'ON' if bwl_value in ('ON', 'TRUE', '1') else 'OFF'}")

        # Apply per-channel attenuation if provided
        if "channels" in settings:
            for ch_str, ch_data in settings["channels"].items():
                if "attenuation" in ch_data:
                    self.write(f"C{int(ch_str)}:ATTN {float(ch_data['attenuation'])}")

        self._logger.info("Settings applied from dictionary")

    def set_output_dir(self, path: str | Path, auto_save: bool = True) -> None:
        """Set output directory for captured data and auto-saved settings.

        Args:
            path: Directory path for output files
            auto_save: If True, automatically save settings on config changes and captures
        """
        self._output_dir = Path(path)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._auto_save_settings = auto_save
        self._logger.info(f"Output directory set to {self._output_dir}")
        if auto_save:
            self._logger.info("Auto-save settings enabled")
            self._save_settings_snapshot()

    def _save_settings_snapshot(self, suffix: str = "") -> Path | None:
        """Save current settings to output directory if auto-save is enabled.

        Args:
            suffix: Optional suffix for filename (e.g., "_capture", "_config")

        Returns:
            Path to saved file, or None if auto-save disabled
        """
        if not self._auto_save_settings or not self._output_dir:
            return None

        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"scope_settings_{timestamp}{suffix}.json"
        filepath = self._output_dir / filename
        self.save_settings(filepath)
        self._logger.info(f"Settings auto-saved to {filepath}")
        return filepath

    # === Offset and Auxiliary Output ===

    def set_offset(
        self,
        channels: dict[int, float] | None = None,
        search: bool = False,
    ) -> dict[int, float]:
        """Set channel offsets.

        Args:
            channels: Dict of channel -> offset (V). If None, uses all active channels.
            search: If True, automatically find signal and set offset.

        Returns:
            Dict of channel -> actual offset set (V).
        """
        if search:
            return self._auto_offset_search()

        if channels is None:
            channels = {ch: 0.0 for ch in self._active_channels}

        for ch, offset in channels.items():
            self.write(f"C{ch}:OFST {offset}")
            self._logger.debug(f"Channel {ch} offset: {offset}V")

        return channels

    def set_auxiliary_output(self, mode: AuxOutputMode) -> None:
        """Set auxiliary output mode.

        Args:
            mode: AuxOutputMode.TRIGGER_OUT or AuxOutputMode.TRIGGER_ENABLED
        """
        try:
            self.write(f"""vbs 'app.Acquisition.AuxOutput.AuxMode = "{mode.value}"' """)
        except Exception:
            # Backward-compatible fallback for models exposing legacy Mode only.
            self.write(f"""vbs 'app.Acquisition.AuxOutput.Mode = "{mode.value}"' """)
        self._logger.info(f"Auxiliary output: {mode.value}")

        # Update settings state
        self._update_settings("auxiliary_output", value=mode.name)

        self._save_settings_snapshot("_aux")

    @abstractmethod
    def _auto_offset_search(self) -> dict[int, float]:
        """Automatically find signal and set offset for each channel."""
        ...

    # === Abstract Methods (implement in subclasses) ===

    @abstractmethod
    def _configure_display(self, display: bool = False) -> None:
        """Configure display settings.

        Args:
            display: Keep display on (True) or off for faster acquisition (False)
        """
        ...

    @abstractmethod
    def _configure_timebase(self, config: AcquisitionConfig) -> None:
        """Configure timebase settings."""
        ...

    @abstractmethod
    def _configure_channel(self, channel: int, config: ChannelConfig) -> None:
        """Configure single channel."""
        ...

    @abstractmethod
    def _configure_sequence(self, config: SequenceConfig) -> None:
        """Configure sequence mode."""
        ...

    def configure_sequence(self, config: SequenceConfig) -> None:
        """Configure sequence mode (public method).

        Args:
            config: Sequence mode configuration
        """
        self._configure_sequence(config)
        self._sequence_config = config

        # Update settings state
        self._settings["sequence"] = {
            "enabled": config.enabled,
            "num_segments": config.num_segments,
            "timeout_enabled": config.timeout_enabled,
            "timeout_seconds": config.timeout_seconds,
        }

        self._save_settings_snapshot("_sequence")

    # === Trigger ===

    def set_trigger(
        self,
        config: TriggerConfig,
        *,
        verify_transition: bool = True,
        apply_mode: bool = True,
    ) -> None:
        """Configure trigger settings."""
        self._trigger_config = config
        self._setup_trigger_source(config)
        self._setup_trigger_level(config)
        if apply_mode:
            self.set_trigger_mode(config.mode, verify_transition=verify_transition)

        # Log trigger configuration
        active = [f"CH{ch}:{t.state.name}" for ch, t in config.channels.items()
                  if t.state != TriggerState.DONT_CARE]
        self._logger.info(f"Trigger configured: {', '.join(active) or 'none'}")

        # Update settings state - trigger
        trigger_channels = {}
        for ch, ch_trig in config.channels.items():
            # Calculate actual level (absolute or relative)
            if ch_trig.level is not None:
                level = ch_trig.level
            else:
                level = ch_trig.level_offset  # Store offset value

            trigger_channels[str(ch)] = {
                "state": ch_trig.state.name,
                "level": level,
            }

        self._settings["trigger"] = {
            "channels": trigger_channels,
            "mode": config.mode,
            "logic": config.logic,
            "external": config.external,
            "external_level": config.external_level,
        }

        self._save_settings_snapshot("_trigger")

    def arm(self, force: bool = False, verify_transition: bool = True) -> None:
        """Arm trigger and wait for acquisition."""
        self._save_settings_snapshot("_capture")
        # Clear previous sweep/waveform data before starting a new acquisition.
        self.write("CLSW")
        if force:
            self.set_run_state("NORM", verify_transition=verify_transition)
            self.write("FRTR")  # Force trigger
            self._logger.debug("Forced trigger")
        else:
            mode = self._trigger_config.mode if self._trigger_config else "SINGLE"
            self.set_run_state(mode, verify_transition=verify_transition)
            self._logger.debug(f"Armed with mode {mode}, waiting for trigger")

    def is_triggered(self) -> bool:
        """Check if trigger has occurred."""
        return "STOP" in self.query("TRMD?")

    def wait_for_trigger(self, timeout: float | None = None, force: bool = False) -> None:
        """Wait for trigger with optional timeout.

        Args:
            timeout: Maximum time to wait in seconds
            force: If True, use force trigger (useful for testing without signal)
        """
        start = time.time()

        # For sequence mode with force trigger - fill all segments immediately
        if force and self._sequence_config and self._sequence_config.enabled:
            num_segments = self._sequence_config.num_segments
            for i in range(num_segments):
                self.write("FRTR")
                time.sleep(0.01)  # Small delay between force triggers
                if timeout and (time.time() - start) > timeout:
                    raise ScopeTimeoutError(f"Trigger timeout: {timeout}s")
            self.set_run_state("STOP", sequence_operation=True)
            self._logger.debug(f"Forced {num_segments} triggers for sequence capture")
            return

        # For sequence mode, poll for completion instead of blocking WAIT
        # This allows graceful timeout with partial data recovery
        if self._sequence_config and self._sequence_config.enabled:
            while True:
                # Check if acquisition is complete (TRMD returns STOP when done)
                trmd = self._read_run_state()
                if "STOP" in trmd:
                    self._logger.debug("Sequence acquisition complete")
                    break
                if timeout and (time.time() - start) > timeout:
                    # Timeout: stop acquisition and keep partial data
                    self.set_run_state("STOP", sequence_operation=True)
                    self._logger.warning(f"Sequence timeout after {timeout}s - partial data available")
                    break
                time.sleep(0.01)
            return

        # For normal mode, poll TRMD status
        while not self.is_triggered():
            if timeout and (time.time() - start) > timeout:
                raise ScopeTimeoutError(f"Trigger timeout: {timeout}s")
            time.sleep(0.01)

    def set_trigger_mode(
        self,
        mode: Literal["AUTO", "NORM", "SINGLE", "STOP"],
        *,
        verify_transition: bool = True,
    ) -> None:
        """Set trigger sweep mode."""
        self.set_run_state(mode, verify_transition=verify_transition)
        self._logger.debug(f"Trigger mode: {mode}")

        # Update settings state
        if "trigger" not in self._settings:
            self._settings["trigger"] = {}
        self._settings["trigger"]["mode"] = mode
        # Keep trigger config in sync so arm() does not overwrite user-selected mode.
        if self._trigger_config is not None:
            self._trigger_config.mode = mode

    @abstractmethod
    def _setup_trigger_source(self, config: TriggerConfig) -> None:
        """Setup trigger source and slope."""
        ...

    @abstractmethod
    def _setup_trigger_level(self, config: TriggerConfig) -> None:
        """Setup trigger levels for each channel."""
        ...

    @abstractmethod
    def _measure_baseline(self, channel: int) -> float:
        """Measure baseline voltage for trigger level calculation."""
        ...

    # === Readout ===

    def _disable_measurement_and_math(self) -> None:
        """Best-effort disable measurement parameters and math traces."""
        self._disable_measurement_slots(range(1, self.MAX_MEASUREMENT_PARAMS + 1))

        for f_idx in range(1, self.MAX_MATH_TRACES + 1):
            scpi_cmd = f"F{f_idx}:TRACE OFF"
            vbs_cmd = f"""vbs 'app.Math.F{f_idx}.View = false' """
            try:
                self.write(scpi_cmd)
            except Exception as exc:
                self._logger.debug("Ignoring math disable failure (%s): %s", scpi_cmd, exc)
            try:
                self.write(vbs_cmd)
            except Exception as exc:
                self._logger.debug("Ignoring math disable failure (%s): %s", vbs_cmd, exc)

    def _disable_measurement_slots(self, slots: range | list[int] | tuple[int, ...]) -> None:
        for p_idx in slots:
            vbs_cmd = f"""vbs 'app.Measure.P{p_idx}.View = false' """
            try:
                self.write(vbs_cmd)
            except Exception as exc:
                self._logger.debug(
                    "Ignoring measurement disable failure (%s): %s",
                    vbs_cmd,
                    exc,
                )

    def disable_measurement_and_math(self) -> None:
        """Disable all measurement parameters and math traces."""
        self._disable_measurement_and_math()

    def readout(
        self,
        channels: list[int] | None = None,
        *,
        keep_measurement_math: bool = False,
    ) -> dict[int, WaveformData]:
        """Read waveform data from specified channels."""
        if not keep_measurement_math:
            self._disable_measurement_and_math()

        channels = channels or list(self._channel_configs.keys())
        result: dict[int, WaveformData] = {}

        trigger_time = self._get_trigger_time()

        # Re-enforce WFSU before reading to ensure data size is correct
        if self._acquisition_config:
            acq = self._acquisition_config
            num_points = self._calculate_num_points(acq)
            
            for ch in channels:
                # 0. Reset WFSU to see full memory
                # NP=0 means "all points", SP=1 means "no sparsification"
                self.write(f"C{ch}:WFSU SP,1,NP,0,FP,0,SN,0")

                # 1. Check what the scope actually captured
                inspect = self.query(f"C{ch}:INSPECT? 'WAVEDESC'")
                actual_points, sample_width, byte_order = self._parse_wavedesc_metadata(inspect)
                
                # 2. Calculate sparsification
                sparsification = 1
                if actual_points > 0:
                    sparsification = max(1, int(actual_points / num_points))

                # 3. Configure WFSU with SP
                # Reset FP to 0 to ensure we start from the beginning of the buffer
                first_point = 0 

                self._logger.debug(f"CH{ch}: Actual={actual_points}, Target={num_points}, SP={sparsification}")
                
                # Note: We request num_points. If SP=100, scope sends num_points * SP range decimated.
                self.write(f"C{ch}:WFSU SP,{sparsification},NP,{num_points},FP,{first_point},SN,0")
                
                # Small delay to ensure setting sticks
                time.sleep(0.05)

                # 4. Read data
                expected_points = min(num_points, actual_points) if actual_points > 0 else None
                raw = self._read_channel_data(
                    ch,
                    count=num_points,
                    sample_width_bytes=sample_width,
                    byte_order=byte_order,
                    expected_points=expected_points,
                )
                dx, x0, dy, y0 = self._get_waveform_scaling(ch)
                
                # If sparsified, adjust dx (time step)
                if sparsification > 1:
                     dx *= sparsification

                result[ch] = WaveformData(
                    raw_data=raw,
                    channel=ch,
                    segment=0,
                    dx=dx,
                    x0=x0,
                    dy=dy,
                    y0=y0,
                    trigger_time=trigger_time,
                    sample_width_bytes=sample_width,
                    byte_order=byte_order,
                    points=len(raw) // sample_width if sample_width else len(raw),
                )

        self._logger.debug(f"Readout complete: channels {channels}")
        return result

    def readout_sequence(
        self,
        channels: list[int] | None = None,
        *,
        keep_measurement_math: bool = False,
    ) -> dict[int, SequenceData]:
        """Read sequence mode data from specified channels."""
        if not keep_measurement_math:
            self._disable_measurement_and_math()

        channels = channels or list(self._channel_configs.keys())
        result: dict[int, SequenceData] = {}

        if not self._sequence_config or not self._sequence_config.enabled:
            raise ScopeConfigurationError("Sequence mode not enabled")

        for ch in channels:
            segments = self._read_sequence_segments(ch)
            result[ch] = SequenceData(segments=tuple(segments), channel=ch)

        self._logger.debug(f"Sequence readout complete: channels {channels}")
        return result

    @abstractmethod
    def _read_channel_data(
        self,
        channel: int,
        offset: int = 0,
        count: int = 0,
        *,
        sample_width_bytes: Literal[1, 2] = 1,
        byte_order: Literal["little", "big"] = "little",
        expected_points: int | None = None,
    ) -> bytes:
        """Read raw waveform data from channel.
        
        Args:
            channel: Channel number
            offset: Start index (0-based)
            count: Number of points to read (0 = all)
            sample_width_bytes: Width of each sample in bytes
            byte_order: Byte order for multi-byte samples
            expected_points: Optional expected sample count for validation
        """
        ...

    @abstractmethod
    def _get_waveform_scaling(
        self, channel: int
    ) -> tuple[float, float, float, float]:
        """Get scaling factors (dx, x0, dy, y0) for channel."""
        ...

    @abstractmethod
    def _get_trigger_time(self) -> str | None:
        """Get trigger timestamp."""
        ...

    @abstractmethod
    def _read_sequence_segments(self, channel: int) -> list[WaveformData]:
        """Read all sequence segments for channel."""
        ...


# === WP804HD ===

class WP804HD(TeledyneLecroyScope):
    """Teledyne LeCroy WP804HD series oscilloscope."""

    MAX_SAMPLING_RATE: float = 20e9   # WP804HD single/dual-channel max
    MIN_MEMORY_SIZE: int = 500
    MAX_MEMORY_SIZE: int | None = 2_500_000
    # Empirical hardware behavior: 10k segments can sustain 500 pts/segment.
    # Keep sequence total memory limit aligned with instrument reality.
    MAX_SEQUENCE_MEMORY_SIZE: int | None = 5_000_000
    MAX_SAMPLING_RATE_BY_ACTIVE_CHANNELS: dict[int, float] | None = {
        1: 20e9,
        2: 20e9,
        3: 10e9,
        4: 10e9,
    }
    MAX_BANDWIDTH: float = 8e9        # 8 GHz
    ADC_BITS: int = 8

    def _configure_display(self, display: bool = False) -> None:
        """Configure display for remote operation."""
        # Reset scope to factory defaults for clean state
        self.write("*RST")
        self.wait_opc()

        if display:
            self.write("DISP ON")
        else:
            self.write("DISP OFF")
        self.write("CHDR OFF")
        self.write("GRID QUATTRO")

    def _configure_timebase(self, config: AcquisitionConfig) -> None:
        """Configure timebase and memory."""
        # Calculate/apply memory size.
        if config.max_samples is not None:
            memory_size = int(config.max_samples)
        else:
            memory_size = int(self._calculate_memory_size(config))
            if memory_size < self.MIN_MEMORY_SIZE:
                memory_size = self.MIN_MEMORY_SIZE

        self.write(f"MSIZ {memory_size}")
        self._vbs_write(
            f"app.Acquisition.Horizontal.HorScale = {config.tdiv}",
            operation="acquisition_horizontal_scale",
            fallback_scpi=f"TDIV {config.tdiv}",
        )
        self.write(f"TRDL {config.trigger_delay}")

        if config.acquisition_mode == "fixed_sample_rate":
            # In fixed-sample-rate mode, lock the sample rate explicitly.
            desired_sr = 1.0 / config.sampling_period
            self._logger.debug("Setting SampleRate to %.0e", desired_sr)
            self.write(fr"""vbs 'app.Acquisition.Horizontal.SampleRate = {desired_sr}' """)
            sr_after = self.query(r"""vbs? 'return=app.Acquisition.Horizontal.SampleRate' """)
            self._logger.debug("SampleRate (after VBS) -> %s", sr_after.strip())

        # Verify TDIV and MSIZ actually stuck
        actual_tdiv = self.query("TDIV?")
        actual_msiz = self.query("MSIZ?")
        self._logger.debug(
            "Config check: TDIV=%s, MSIZ=%s",
            actual_tdiv.strip(),
            actual_msiz.strip(),
        )

        # Check VBS internal values
        vbs_npts = self.query(r"""vbs? 'return=app.Acquisition.Horizontal.NumPoints' """)
        self._logger.debug("VBS NumPoints: %s", vbs_npts.strip())

        # Waveform setup for all channels.
        num_points = max(1, int(memory_size))
        self._logger.debug("Calculated num_points for WFSU: %s", num_points)
        sparsification = 1
        effective_sampling_period = self.TIME_DIVISIONS * config.tdiv / float(num_points)
        first_point = int(
            config.window_delay / effective_sampling_period
        ) - (num_points // 2)

        for ch in self._active_channels:
            self.write(
                f"C{ch}:WFSU SP,{sparsification},NP,{num_points},"
                f"FP,{first_point},SN,0"
            )

        self._logger.info(f"Timebase: TDIV={config.tdiv}, points={num_points}")

    def _configure_channel(self, channel: int, config: ChannelConfig) -> None:
        """Configure single channel."""
        ch = f"C{channel}"
        view_value = "-1" if config.enabled else "0"
        self._vbs_write(
            f"app.Acquisition.C{channel}.View = {view_value}",
            operation="channel_view",
            fallback_scpi=f"{ch}:TRACE {'ON' if config.enabled else 'OFF'}",
        )
        self._vbs_write(
            f"app.Acquisition.C{channel}.VerScale = {config.vdiv}",
            operation="channel_scale",
            fallback_scpi=f"{ch}:VDIV {config.vdiv}",
        )
        self._vbs_write(
            f"app.Acquisition.C{channel}.VerOffset = {config.offset}",
            operation="channel_offset",
            fallback_scpi=f"{ch}:OFST {config.offset}",
        )
        # Some firmware accepts VBS write but does not actually update offset.
        # Force SCPI write to guarantee readback consistency.
        self.write(f"{ch}:OFST {config.offset}")
        self._vbs_write(
            f'app.Acquisition.C{channel}.Coupling = "{config.coupling.value}"',
            operation="channel_coupling",
            fallback_scpi=f"{ch}:CPL {config.coupling.value}",
        )
        self.write(f"{ch}:ATTN 1")
        self.write("BWL OFF")

        self._logger.debug(f"Channel {channel} configured: vdiv={config.vdiv}")

    def _configure_sequence(self, config: SequenceConfig) -> None:
        """Configure sequence mode."""
        self._vbs_write(
            f"app.Acquisition.Horizontal.NumSegments = {config.num_segments}",
            operation="acquisition_num_segments",
            fallback_scpi=f"SEQ ON,{config.num_segments},2.5E+6",
        )
        self.write(f"SEQ ON,{config.num_segments},2.5E+6")
        self._logger.info(f"Sequence mode: {config.num_segments} segments")

        if config.timeout_enabled:
            self._vbs_write(
                "app.Acquisition.Horizontal.SequenceTimeoutEnable = -1",
                operation="acquisition_sequence_timeout",
                fallback_scpi=None,
            )
            self._vbs_write(
                f"app.Acquisition.Horizontal.SequenceTimeout = {config.timeout_seconds}",
                operation="acquisition_sequence_timeout",
                fallback_scpi=f"SEQ ON,{config.num_segments},{config.timeout_seconds}",
            )
        else:
            self._vbs_write(
                "app.Acquisition.Horizontal.SequenceTimeoutEnable = 0",
                operation="acquisition_sequence_timeout",
                fallback_scpi=None,
            )

    def _get_trigger_channels(self, config: TriggerConfig) -> dict[int, ChannelTrigger]:
        """Get channel trigger settings."""
        return config.channels

    def _setup_trigger_source(self, config: TriggerConfig) -> None:
        """Setup trigger source with pattern trigger.

        WP804HD firmware rejects VBS Pattern.* writes in some revisions.
        Use SCPI TRPA command path directly for model robustness.
        """
        channels = self._get_trigger_channels(config)

        states = ["X"] * 4
        for ch, ch_trig in channels.items():
            if 1 <= ch <= 4:
                states[ch - 1] = ch_trig.state.value

        logic_value = config.logic.upper() if config.logic.upper() in {"OR", "AND"} else "OR"
        trpa_parts: list[str] = []
        for idx, state in enumerate(states, start=1):
            trpa_parts.extend([f"C{idx}", state])
        # Some firmware rejects explicit "EX,X" in TRPA.
        if config.external:
            trpa_parts.extend(["EX", "H"])
        trpa_parts.extend(["STATE", logic_value])
        trpa_command = "TRPA " + ",".join(trpa_parts)
        has_internal_channel_trigger = any(state != "X" for state in states)
        if config.external and not has_internal_channel_trigger:
            # External-only trigger is more robust via explicit edge-source select.
            self.write("TRSE EDGE,SR,EX")
            return
        self.write(trpa_command)
        # Ensure trigger source follows the intended internal channel.
        if not config.external:
            for idx, state in enumerate(states, start=1):
                if state != "X":
                    self.write(f"TRSE EDGE,SR,C{idx}")
                    break

        if config.external:
            self._logger.debug(
                f"Trigger pattern: C1={states[0]}, C2={states[1]}, C3={states[2]}, C4={states[3]}, EX=H"
            )
        else:
            self._logger.debug(
                f"Trigger pattern: C1={states[0]}, C2={states[1]}, C3={states[2]}, C4={states[3]}"
            )

    def _setup_trigger_level(self, config: TriggerConfig) -> None:
        """Setup trigger level for each channel (absolute or relative to baseline)."""
        channels = self._get_trigger_channels(config)

        for ch, ch_trig in channels.items():
            if ch_trig.state == TriggerState.DONT_CARE:
                continue  # Skip channels not used for triggering

            if ch_trig.level is not None:
                # Use absolute level directly
                level = ch_trig.level
                self._logger.debug(f"Channel {ch} trigger level (absolute): {level}")
            else:
                # Measure baseline and add offset
                baseline = self._measure_baseline(ch)
                level = baseline + ch_trig.level_offset
                self._logger.debug(f"Channel {ch} trigger level (baseline={baseline}, offset={ch_trig.level_offset}): {level}")

            self.write(f"C{ch}:TRLV {level}")

        # External trigger level
        if config.external:
            self.write(f"EX:TRLV {config.external_level}")
            self._logger.debug(f"External trigger level: {config.external_level}V")

    # Constants for offset search
    _MIN_SIGNAL = 0.001      # V minimum amplitude to detect signal
    _V_DIVISION = 6          # Divisions to scan for signal
    _SHIFT_DIVISION = 2.5    # Divisions to shift after finding signal
    _MAX_OFFSET = -1.0       # V maximum offset before giving up

    def _auto_offset_search(self) -> dict[int, float]:
        """Automatically find signal and set offset for each channel."""
        offsets = {}

        for ch in self._active_channels:
            cfg = self._channel_configs.get(ch)
            vdiv = cfg.vdiv if cfg else 0.020

            try:
                # Setup measurements for this channel
                self.write(f"PACU 1,MEAN,C{ch}")  # Baseline
                self.write(f"PACU 2,AMPL,C{ch}")  # Amplitude

                # Start with zero offset
                initial_offset = 0.0
                self.write(f"C{ch}:OFFSET 0")

                # Scan for signal
                self.write("TRMD AUTO")
                time.sleep(1.0)
                self.write("TRMD STOP")

                amplitude_response = self.query(
                    r"""vbs? 'return=app.measure.p2.out.result.value' """
                )

                # Try to find signal by scanning offsets
                iteration = 0
                while True:
                    try:
                        amplitude = float(amplitude_response.split()[-1])
                        if amplitude >= self._MIN_SIGNAL:
                            break
                    except (ValueError, IndexError):
                        pass

                    # Move offset to search for signal
                    initial_offset -= vdiv * self._V_DIVISION
                    if initial_offset < self._MAX_OFFSET:
                        self._logger.warning(
                            f"Channel {ch}: Could not find signal, using offset=0"
                        )
                        offsets[ch] = 0.0
                        break

                    self.write(f"C{ch}:OFFSET {initial_offset}")
                    self.write("TRMD AUTO")
                    time.sleep(1.0)
                    self.write("TRMD NORM")

                    amplitude_response = self.query(
                        r"""vbs? 'return=app.measure.p2.out.result.value' """
                    )

                    iteration += 1
                    if iteration > 10:
                        self._logger.warning(
                            f"Channel {ch}: Max iterations, using offset=0"
                        )
                        offsets[ch] = 0.0
                        break
                else:
                    continue

                # Found signal - measure baseline and set offset
                baseline_response = self.query(
                    r"""vbs? 'return=app.measure.p1.out.result.value' """
                )
                try:
                    baseline = float(baseline_response.split()[-1])
                except (ValueError, IndexError):
                    baseline = 0.0

                # Shift signal to visible area
                offset = vdiv * self._SHIFT_DIVISION - baseline
                self.write(f"C{ch}:OFFSET {offset}")
                offsets[ch] = -offset  # Return positive offset value

                self._logger.info(f"Channel {ch}: auto offset = {-offset:.4f}V")
            finally:
                self._disable_measurement_slots((1, 2))

        self.write("TRMD NORM")
        return offsets

    def _measure_baseline(self, channel: int) -> float:
        """Measure baseline using parameter measurement."""
        try:
            self.write(f"PACU 1,MEAN,C{channel}")

            # Trigger briefly to get measurement
            self.write("TRMD AUTO")
            time.sleep(0.5)
            self.write("TRMD NORM")

            response = self.query(
                r"""vbs? 'return=app.measure.p1.out.result.value' """
            )
            self._logger.debug(f"Baseline measurement response: {response}")

            # Extract numeric value from response
            # Response format can vary; try to find a valid float
            for part in response.split():
                try:
                    return float(part)
                except ValueError:
                    continue

            # If no numeric value found, use 0 as baseline (DC level)
            self._logger.warning(
                f"Could not parse baseline for C{channel}, using 0. "
                f"Response was: {response}"
            )
            return 0.0
        finally:
            self._disable_measurement_slots((1,))

    def _read_channel_data(
        self,
        channel: int,
        offset: int = 0,
        count: int = 0,
        *,
        sample_width_bytes: Literal[1, 2] = 1,
        byte_order: Literal["little", "big"] = "little",
        expected_points: int | None = None,
    ) -> bytes:
        """Read raw waveform data.

        Strips IEEE 488.2 definite-length binary block header:
        Format: #<n><length><data><terminator>
        where <n> is digit count, <length> is byte count in <n> digits
        """
        if count > 0:
            # Request specific range: DAT1,NO,<offset>,NP,<count>
            cmd = f"C{channel}:WF? DAT1,NO,{offset},NP,{count}"
        else:
            # Request all data (relies on WFSU or default)
            cmd = f"C{channel}:WF? DAT1"
            
        self.write(cmd)
        raw = self.read_raw()
        return self._strip_binary_header(
            raw,
            sample_width_bytes=sample_width_bytes,
            byte_order=byte_order,
            expected_points=expected_points,
        )

    def _strip_binary_header(
        self,
        data: bytes,
        *,
        sample_width_bytes: Literal[1, 2] = 1,
        byte_order: Literal["little", "big"] = "little",
        expected_points: int | None = None,
    ) -> bytes:
        """Strip IEEE 488.2 binary block header from raw data.

        Args:
            data: Raw bytes including header

        Returns:
            Waveform data with header and terminator removed
        """
        try:
            parsed = parse_ieee4882_block(
                data,
                sample_width_bytes=sample_width_bytes,
                byte_order=byte_order,
                expected_points=expected_points,
            )
            return parsed.payload
        except ValueError as exc:
            header_preview = data[:16].hex()
            self._vbs_logger.warning(
                "block_parse_failed header=%s len=%d reason=%s",
                header_preview,
                len(data),
                exc,
            )
            raise ScopeConfigurationError(f"Failed to parse waveform block: {exc}") from exc

    def _get_waveform_scaling(
        self, channel: int
    ) -> tuple[float, float, float, float]:
        """Get waveform scaling factors.

        If configure() was not called, reads values directly from scope.
        """
        # Get sampling period (dx)
        if self._acquisition_config is not None:
            dx = self._acquisition_config.sampling_period
        else:
            # Read from scope: calculate from TDIV and memory size
            acq = self.read_acquisition_config()
            dx = acq.sampling_period

        # Get time origin (x0) - always read from scope for accuracy
        tdiv = self._parse_numeric_response(self.query("TDIV?"))
        trdl = self._parse_numeric_response(self.query("TRDL?"))
        x0 = self.X0_DIVISION * tdiv + trdl

        # Get voltage scaling (dy, y0)
        dy = self._parse_numeric_response(
            self.query(f"C{channel}:VDIV?")
        ) * self.DY_ADC_CONVERSION
        y0 = -self._parse_numeric_response(self.query(f"C{channel}:OFFSET?"))

        return dx, x0, dy, y0

    def _get_trigger_time(self) -> str | None:
        """Get trigger timestamp."""
        trg_time = self.query("C1:INSPECT? TRIGGER_TIME")
        match = re.search(
            r"(?<=Time = )\s*\d+:\s*\d+:\s*\d+\.\d+", trg_time
        )
        return match.group(0) if match else None

    def _read_sequence_segments(self, channel: int) -> list[WaveformData]:
        """Read all sequence segments."""
        if not self._sequence_config:
            return []

        # Calculate target points per segment and per channel scaling.
        acq_config = self._acquisition_config or self.read_acquisition_config()
        num_points = self._calculate_num_points(acq_config)
        num_segments = self._sequence_config.num_segments
        if num_points <= 0 or num_segments <= 0:
            return []

        dx, x0, dy, y0 = self._get_waveform_scaling(channel)

        # Configure WFSU once to request all sequence segments in one transfer.
        self.write(f"C{channel}:WFSU SP,1,NP,0,FP,0,SN,0")
        inspect = self.query(f"C{channel}:INSPECT? 'WAVEDESC'")
        actual_points, sample_width, byte_order = self._parse_wavedesc_metadata(inspect)
        if actual_points <= 0:
            self._logger.debug(f"CH{channel}: no sequence data available")
            return []

        total_target_points = num_points * num_segments
        sparsification = max(1, int(actual_points / total_target_points)) if actual_points > 0 else 1
        self.write(
            f"C{channel}:WFSU SP,{sparsification},NP,{total_target_points},FP,0,SN,0"
        )

        raw = self._read_channel_data(
            channel,
            count=total_target_points,
            sample_width_bytes=sample_width,
            byte_order=byte_order,
            # Allow partial sequence on timeout; segment split handles this.
            expected_points=None,
        )

        bytes_per_segment = num_points * sample_width
        split_bytes = bytes_per_segment
        if num_segments > 0:
            even_split_bytes = len(raw) // num_segments
            if self._acquisition_config is None:
                # If acquisition config was not preloaded, infer segment size
                # from payload and allow a small trailing remainder.
                if even_split_bytes >= sample_width:
                    split_bytes = even_split_bytes - (even_split_bytes % sample_width)
                    if split_bytes < sample_width:
                        split_bytes = sample_width
            elif len(raw) % num_segments == 0 and even_split_bytes >= sample_width:
                split_bytes = even_split_bytes

        full_segments = min(num_segments, len(raw) // split_bytes)
        if full_segments <= 0:
            return []

        seg_dx = dx * sparsification
        segments: list[WaveformData] = []
        for seg_idx in range(full_segments):
            start = seg_idx * split_bytes
            end = start + split_bytes
            seg_raw = raw[start:end]
            segments.append(
                WaveformData(
                    raw_data=seg_raw,
                    channel=channel,
                    segment=seg_idx,
                    dx=seg_dx,
                    x0=x0,
                    dy=dy,
                    y0=y0,
                    sample_width_bytes=sample_width,
                    byte_order=byte_order,
                    points=len(seg_raw) // sample_width if sample_width else len(seg_raw),
                )
            )

        return segments


# === WR8208HD ===

class WR8208HD(WP804HD):
    """Teledyne LeCroy WR8208HD series oscilloscope.

    Inherits from WP804HD as SCPI commands are mostly identical.
    Only hardware specs differ.
    """

    MAX_SAMPLING_RATE: float = 40e9   # 40 GS/s
    MIN_MEMORY_SIZE: int = 500
    MAX_MEMORY_SIZE: int | None = None
    MAX_SEQUENCE_MEMORY_SIZE: int | None = None
    MAX_MEMORY_SIZE_BY_ACTIVE_CHANNELS: dict[int, int] | None = {
        8: 250_000_000,
        4: 500_000_000,
        2: 1_000_000_000,
    }
    MAX_SEQUENCE_MEMORY_SIZE_BY_ACTIVE_CHANNELS: dict[int, int] | None = {
        8: 50_000_000,
        4: 100_000_000,
        2: 200_000_000,
    }
    MAX_SAMPLING_RATE_BY_ACTIVE_CHANNELS: dict[int, float] | None = {
        8: 10e9,
        4: 20e9,
        2: 40e9,
        1: 40e9,
    }
    MAX_BANDWIDTH: float = 4e9        # 4 GHz
    ADC_BITS: int = 8
    MAX_CHANNELS: int = 8

    def __init__(
        self,
        address: str,
        protocol: Literal["lxi", "vicp"] = "lxi",
        timeout: float = 30.0,
        active_channels: list[int] | None = None,
    ) -> None:
        if active_channels is None:
            active_channels = list(range(1, self.MAX_CHANNELS + 1))
        super().__init__(
            address=address,
            protocol=protocol,
            timeout=timeout,
            active_channels=active_channels,
        )

    def _setup_trigger_source(self, config: TriggerConfig) -> None:
        """Setup trigger source with SCPI pattern trigger for up to 8 channels."""
        channels = self._get_trigger_channels(config)

        states = ["X"] * self.MAX_CHANNELS
        for ch, ch_trig in channels.items():
            if 1 <= ch <= self.MAX_CHANNELS:
                states[ch - 1] = ch_trig.state.value

        logic_value = config.logic.upper() if config.logic.upper() in {"OR", "AND"} else "OR"
        trpa_parts: list[str] = []
        for idx, state in enumerate(states, start=1):
            trpa_parts.extend([f"C{idx}", state])
        # Some WR8208HD firmware rejects explicit "EX,X" in TRPA.
        if config.external:
            trpa_parts.extend(["EX", "H"])
        trpa_parts.extend(["STATE", logic_value])
        trpa_command = "TRPA " + ",".join(trpa_parts)
        has_internal_channel_trigger = any(state != "X" for state in states)
        if config.external and not has_internal_channel_trigger:
            # External-only trigger is more robust via explicit edge-source select.
            self.write("TRSE EDGE,SR,EX")
            return
        try:
            self.write(trpa_command)
        except Exception as exc:
            # Some WR8208HD firmware rejects TRPA when using external-only trigger.
            # Fall back to explicit edge-source selection for EX trigger.
            if config.external and not has_internal_channel_trigger:
                self._logger.warning(
                    "TRPA external-only trigger rejected; falling back to TRSE EDGE,SR,EX: %s",
                    exc,
                )
                self.write("TRSE EDGE,SR,EX")
                return
            raise
        # Ensure trigger source follows the intended internal channel.
        if not config.external:
            for idx, state in enumerate(states, start=1):
                if state != "X":
                    self.write(f"TRSE EDGE,SR,C{idx}")
                    break

    def _get_waveform_scaling(
        self, channel: int
    ) -> tuple[float, float, float, float]:
        """Get waveform scaling factors with WR-local fast path.

        WR8208HD sequence readout can involve very large payloads (2k-5k+ segments).
        When configuration is already known locally, avoid extra SCPI queries
        (`TDIV?`, `TRDL?`, `C#:VDIV?`, `C#:OFFSET?`) per channel.
        """
        acq = self._acquisition_config
        ch_cfg = self._channel_configs.get(channel)
        if acq is not None and ch_cfg is not None:
            dx = acq.sampling_period
            x0 = self.X0_DIVISION * acq.tdiv + acq.trigger_delay
            dy = ch_cfg.vdiv * self.DY_ADC_CONVERSION
            y0 = -ch_cfg.offset
            return dx, x0, dy, y0
        return super()._get_waveform_scaling(channel)

    def readout_sequence(
        self,
        channels: list[int] | None = None,
        *,
        sn_mode: Literal["auto", "all", "loop", "batch"] = "auto",
        batch_segments: int = 100,
        keep_measurement_math: bool = False,
    ) -> dict[int, SequenceData]:
        """Read sequence data with optional SN strategy.

        Args:
            channels: Channels to read (defaults to configured channels).
            sn_mode:
                - "all": Use SN=0 bulk transfer (default legacy behavior)
                - "loop": Read SN=1..N segment-by-segment
                - "batch": Read SN=0 transfer in offset/count batches
                - "auto": Choose "batch" for large segment count, else "all"
            batch_segments: Segment count per batch when sn_mode="batch".
        """
        if sn_mode not in {"auto", "all", "loop", "batch"}:
            raise ScopeConfigurationError(f"Invalid sn_mode: {sn_mode}")
        if not self._sequence_config or not self._sequence_config.enabled:
            raise ScopeConfigurationError("Sequence mode not enabled")
        if batch_segments <= 0:
            raise ScopeConfigurationError("batch_segments must be > 0")

        t0 = time.perf_counter()
        effective_mode = sn_mode
        if sn_mode == "auto":
            # Large SN=0 payloads can incur long scope-side packaging stalls.
            effective_mode = "batch" if self._sequence_config.num_segments >= 1000 else "all"

        if effective_mode == "all":
            result = super().readout_sequence(
                channels=channels,
                keep_measurement_math=keep_measurement_math,
            )
            self._last_sequence_profile = {
                "mode": effective_mode,
                "channels": list(result.keys()),
                "channel_metrics": {},
                "total_ms": (time.perf_counter() - t0) * 1000.0,
            }
            return result

        if not keep_measurement_math:
            self._disable_measurement_and_math()

        channels = channels or list(self._channel_configs.keys())
        result: dict[int, SequenceData] = {}
        channel_metrics: dict[int, dict[str, float | int]] = {}
        for ch in channels:
            if effective_mode == "loop":
                segments = self._read_sequence_segments_loop(ch)
            else:
                segments = self._read_sequence_segments_batch(
                    ch,
                    batch_segments=batch_segments,
                )
            result[ch] = SequenceData(segments=tuple(segments), channel=ch)
            metrics = getattr(self, "_last_sequence_channel_metric", None)
            if isinstance(metrics, dict):
                channel_metrics[ch] = dict(metrics)
        self._logger.debug(
            "Sequence readout complete (WR %s mode): channels %s",
            effective_mode,
            channels,
        )
        self._last_sequence_profile = {
            "mode": effective_mode,
            "batch_segments": batch_segments,
            "channels": list(channels),
            "channel_metrics": channel_metrics,
            "total_ms": (time.perf_counter() - t0) * 1000.0,
        }
        return result

    def _read_sequence_segments_loop(self, channel: int) -> list[WaveformData]:
        """Read sequence using SN=1..N loop to reduce SN=0 packaging stalls."""
        t0 = time.perf_counter()
        if not self._sequence_config:
            self._last_sequence_channel_metric = {"segments": 0, "total_ms": 0.0}
            return []

        acq_config = self._acquisition_config or self.read_acquisition_config()
        num_points = self._calculate_num_points(acq_config)
        num_segments = self._sequence_config.num_segments
        if num_points <= 0 or num_segments <= 0:
            self._last_sequence_channel_metric = {"segments": 0, "total_ms": 0.0}
            return []
        t_scaling0 = time.perf_counter()
        dx, x0, dy, y0 = self._get_waveform_scaling(channel)
        t_scaling1 = time.perf_counter()

        # Probe one segment metadata to determine sample width/order.
        t_meta0 = time.perf_counter()
        self.write(f"C{channel}:WFSU SP,1,NP,0,FP,0,SN,1")
        inspect = self.query(f"C{channel}:INSPECT? 'WAVEDESC'")
        actual_points, sample_width, byte_order = self._parse_wavedesc_metadata(inspect)
        t_meta1 = time.perf_counter()
        if actual_points <= 0:
            self._logger.debug(f"CH{channel}: no sequence data available (loop mode)")
            self._last_sequence_channel_metric = {
                "segments": 0,
                "scaling_ms": (t_scaling1 - t_scaling0) * 1000.0,
                "metadata_ms": (t_meta1 - t_meta0) * 1000.0,
                "transfer_ms": 0.0,
                "split_ms": 0.0,
                "total_ms": (time.perf_counter() - t0) * 1000.0,
            }
            return []

        target_points = min(num_points, actual_points)
        t_xfer0 = time.perf_counter()
        t_split0 = t_xfer0
        t_split1 = t_xfer0
        segments: list[WaveformData] = []
        for sn in range(1, num_segments + 1):
            self.write(f"C{channel}:WFSU SP,1,NP,{target_points},FP,0,SN,{sn}")
            try:
                raw = self._read_channel_data(
                    channel,
                    count=target_points,
                    sample_width_bytes=sample_width,
                    byte_order=byte_order,
                    expected_points=None,
                )
            except Exception as exc:  # noqa: BLE001
                self._logger.debug(
                    "CH%s SN=%s read failed in loop mode: %s", channel, sn, exc
                )
                continue
            if not raw:
                continue
            t_split0 = time.perf_counter()
            segments.append(
                WaveformData(
                    raw_data=raw,
                    channel=channel,
                    segment=sn - 1,
                    dx=dx,
                    x0=x0,
                    dy=dy,
                    y0=y0,
                    sample_width_bytes=sample_width,
                    byte_order=byte_order,
                    points=len(raw) // sample_width if sample_width else len(raw),
                )
            )
            t_split1 = time.perf_counter()
        t_xfer1 = time.perf_counter()
        split_ms = (t_split1 - t_split0) * 1000.0 if segments else 0.0
        self._last_sequence_channel_metric = {
            "segments": len(segments),
            "scaling_ms": (t_scaling1 - t_scaling0) * 1000.0,
            "metadata_ms": (t_meta1 - t_meta0) * 1000.0,
            "transfer_ms": (t_xfer1 - t_xfer0) * 1000.0,
            "split_ms": split_ms,
            "total_ms": (time.perf_counter() - t0) * 1000.0,
        }

        return segments

    def _read_sequence_segments_batch(self, channel: int, *, batch_segments: int) -> list[WaveformData]:
        """Read sequence in point-offset batches while staying in SN=0 mode."""
        t0 = time.perf_counter()
        if not self._sequence_config:
            self._last_sequence_channel_metric = {"segments": 0, "total_ms": 0.0}
            return []

        acq_config = self._acquisition_config or self.read_acquisition_config()
        num_points = self._calculate_num_points(acq_config)
        num_segments = self._sequence_config.num_segments
        if num_points <= 0 or num_segments <= 0:
            self._last_sequence_channel_metric = {"segments": 0, "total_ms": 0.0}
            return []
        t_scaling0 = time.perf_counter()
        dx, x0, dy, y0 = self._get_waveform_scaling(channel)
        t_scaling1 = time.perf_counter()

        # Probe once for payload metadata.
        t_meta0 = time.perf_counter()
        self.write(f"C{channel}:WFSU SP,1,NP,0,FP,0,SN,0")
        inspect = self.query(f"C{channel}:INSPECT? 'WAVEDESC'")
        actual_points, sample_width, byte_order = self._parse_wavedesc_metadata(inspect)
        t_meta1 = time.perf_counter()
        if actual_points <= 0:
            self._logger.debug(f"CH{channel}: no sequence data available (batch mode)")
            self._last_sequence_channel_metric = {
                "segments": 0,
                "scaling_ms": (t_scaling1 - t_scaling0) * 1000.0,
                "metadata_ms": (t_meta1 - t_meta0) * 1000.0,
                "transfer_ms": 0.0,
                "split_ms": 0.0,
                "total_ms": (time.perf_counter() - t0) * 1000.0,
            }
            return []

        total_target_points = num_points * num_segments
        sparsification = max(1, int(actual_points / total_target_points)) if actual_points > 0 else 1
        self.write(
            f"C{channel}:WFSU SP,{sparsification},NP,{total_target_points},FP,0,SN,0"
        )

        batch_points = max(num_points, num_points * batch_segments)
        flat = bytearray()
        point_offset = 0
        t_xfer0 = time.perf_counter()
        while point_offset < total_target_points:
            count = min(batch_points, total_target_points - point_offset)
            chunk = self._read_channel_data(
                channel,
                offset=point_offset,
                count=count,
                sample_width_bytes=sample_width,
                byte_order=byte_order,
                expected_points=None,
            )
            if not chunk:
                break
            flat.extend(chunk)

            points_read = len(chunk) // sample_width if sample_width else len(chunk)
            if points_read <= 0:
                break
            point_offset += points_read
            if points_read < count:
                # Partial payload received; keep what we have.
                break
        t_xfer1 = time.perf_counter()

        raw = bytes(flat)
        bytes_per_segment = num_points * sample_width
        if bytes_per_segment <= 0:
            return []
        full_segments = min(num_segments, len(raw) // bytes_per_segment)
        if full_segments <= 0:
            return []

        seg_dx = dx * sparsification
        segments: list[WaveformData] = []
        t_split0 = time.perf_counter()
        for seg_idx in range(full_segments):
            start = seg_idx * bytes_per_segment
            end = start + bytes_per_segment
            seg_raw = raw[start:end]
            segments.append(
                WaveformData(
                    raw_data=seg_raw,
                    channel=channel,
                    segment=seg_idx,
                    dx=seg_dx,
                    x0=x0,
                    dy=dy,
                    y0=y0,
                    sample_width_bytes=sample_width,
                    byte_order=byte_order,
                    points=len(seg_raw) // sample_width if sample_width else len(seg_raw),
                )
            )
        t_split1 = time.perf_counter()
        self._last_sequence_channel_metric = {
            "segments": len(segments),
            "scaling_ms": (t_scaling1 - t_scaling0) * 1000.0,
            "metadata_ms": (t_meta1 - t_meta0) * 1000.0,
            "transfer_ms": (t_xfer1 - t_xfer0) * 1000.0,
            "split_ms": (t_split1 - t_split0) * 1000.0,
            "total_ms": (time.perf_counter() - t0) * 1000.0,
        }

        return segments

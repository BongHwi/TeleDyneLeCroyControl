# lecroy_visa.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import logging

import pyvisa
from pyvisa.resources import MessageBasedResource


# ============================================================
# Human-friendly parameter registry (static ones)
# ============================================================

PARAMS: Dict[str, str] = {
    # ----- Horizontal / acquisition -----
    "horizontal.sample_mode": "Acquisition.Horizontal.SampleMode",
    "horizontal.num_segments": "Acquisition.Horizontal.NumSegments",
    "horizontal.sequence_timeout_enable": "Acquisition.Horizontal.SequenceTimeoutEnable",
    "horizontal.sequence_timeout": "Acquisition.Horizontal.SequenceTimeout",
    "horizontal.hor_scale": "Acquisition.Horizontal.HorScale",
    "horizontal.hor_offset": "Acquisition.Horizontal.HorOffset",
    "horizontal.num_points": "Acquisition.Horizontal.NumPoints",
    "horizontal.time_per_point": "Acquisition.Horizontal.TimePerPoint",

    # ----- Trigger (common) -----
    "trigger.type": "Acquisition.Trigger.Type",
    "trigger.source": "Acquisition.Trigger.Source",
    "trigger.pattern_type": "Acquisition.Trigger.PatternType",
    "trigger.pattern_level": "Acquisition.Trigger.Pattern.Level",

    # ----- Save waveform -----
    "save.save_to": "SaveRecall.Waveform.SaveTo",
    "save.waveform_dir": "SaveRecall.Waveform.WaveformDir",
    "save.wave_format": "SaveRecall.Waveform.WaveFormat",
    "save.trace_title": "SaveRecall.Waveform.TraceTitle",
    "save.save_source": "SaveRecall.Waveform.SaveSource",

    # ----- AUX OUT (partially dynamic: mode differs by family) -----
    "aux_out.aux_in_coupling": "Acquisition.AuxOutput.AuxInCoupling",
}

# Channel template keys (expanded dynamically)
CH_TEMPLATE = {
    "view": "Acquisition.{ch}.View",
    "scale": "Acquisition.{ch}.VerScale",
    "offset": "Acquisition.{ch}.VerOffset",
    "coupling": "Acquisition.{ch}.Coupling",
    "invert": "Acquisition.{ch}.Invert",
    "bandwidth_limit": "Acquisition.{ch}.BandwidthLimit",
    "deskew": "Acquisition.{ch}.Deskew",
    "units": "Acquisition.{ch}.Units",
}

# Trigger per-source templates (expanded dynamically)
TRIG_SRC_TEMPLATE = {
    "level": "Acquisition.Trigger.{src}.Level",
    "slope": "Acquisition.Trigger.{src}.Slope",
    "pattern_state": "Acquisition.Trigger.{src}.PatternState",
}


def _quote_vbs_value(value: Any) -> str:
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _normalize_aux_mode(v: str) -> str:
    """
    Accept user-friendly variants and map to LeCroy enum strings.
    The manual uses: Off, PassFail, TriggerEnabled, TriggerOut, Square, DCLevel
    """
    s = (v or "").strip()

    # common aliases people type
    aliases = {
        "TRIGGERED_ENABLED": "TriggerEnabled",
        "TRIGGER_ENABLE": "TriggerEnabled",
        "TRIGGER_ENABLED": "TriggerEnabled",
        "TRIGGERENABLED": "TriggerEnabled",
        "TRIGENABLE": "TriggerEnabled",
        "TRIG_ENABLED": "TriggerEnabled",
        "TRIGENABLEd": "TriggerEnabled",
        "TRIGOUT": "TriggerOut",
        "TRIGGER_OUT": "TriggerOut",
        "PASS_FAIL": "PassFail",
        "PASSFAIL": "PassFail",
        "OFF": "Off",
        "SQUARE": "Square",
        "DCLEVEL": "DCLevel",
        "DC_LEVEL": "DCLevel",
    }

    up = s.upper().replace(" ", "")
    if up in aliases:
        return aliases[up]

    # If already in correct camelcase, accept
    return s


@dataclass
class LeCroyVisa:
    address: str
    visa_backend: str = "@py"
    timeout_s: float = 5.0

    def __post_init__(self) -> None:
        self._rm: Optional[pyvisa.ResourceManager] = None
        self._scope: Optional[MessageBasedResource] = None
        self._connected: bool = False
        self._logger = logging.getLogger(__name__)

        self._idn_cache: Optional[str] = None
        self._model_cache: Optional[str] = None
        self._family_cache: Optional[str] = None  # "WRWS" or "WP" or "UNKNOWN"

    # --------------------------
    # Connection / SCPI basics
    # --------------------------
    def connect(self) -> None:
        if self._connected:
            return

        self._rm = pyvisa.ResourceManager(self.visa_backend)
        # LeCroy VXI-11/INSTR
        resource = f"TCPIP0::{self.address}::inst0::INSTR"
        self._scope = self._rm.open_resource(resource, resource_pyclass=MessageBasedResource)
        self._scope.timeout = int(self.timeout_s * 1000)
        self._connected = True

        logging.getLogger("pyvisa").setLevel(logging.WARNING)

        # Warm up IDN cache (also used for AUX OUT family selection)
        try:
            _ = self.idn()
        except Exception:
            # Don't hard-fail connect for this
            pass

    def close(self) -> None:
        try:
            if self._scope is not None:
                self._scope.close()
        finally:
            self._scope = None
            self._connected = False

    def write(self, cmd: str) -> None:
        if self._scope is None:
            raise RuntimeError("Not connected")
        self._scope.write(cmd)

    def query(self, cmd: str) -> str:
        if self._scope is None:
            raise RuntimeError("Not connected")
        return str(self._scope.query(cmd))

    def idn(self) -> str:
        if self._idn_cache is None:
            self._idn_cache = (self.query("*IDN?") or "").strip()
            # Example: "LECROY,WP804HD,LCRY...,11.2.0"
            parts = [p.strip() for p in self._idn_cache.split(",")]
            self._model_cache = parts[1] if len(parts) >= 2 else ""
            self._family_cache = self._infer_family(self._model_cache or "")
        return self._idn_cache

    @property
    def model(self) -> str:
        _ = self.idn()
        return self._model_cache or ""

    @property
    def family(self) -> str:
        _ = self.idn()
        return self._family_cache or "UNKNOWN"

    def _infer_family(self, model: str) -> str:
        m = (model or "").upper()
        # Typical: WP804HD, WR8208HD, WSxxxx
        if m.startswith("WP"):
            return "WP"
        if m.startswith("WR") or m.startswith("WS"):
            return "WRWS"
        return "UNKNOWN"

    # --------------------------
    # VBS helpers
    # --------------------------
    def vbs_call(self, expr: str) -> str:
        script = f"return = {expr}"
        return self.query(f"VBS? '{script}'").strip()

    def vbs_get(self, path: str) -> str:
        script = f"return = app.{path}"
        return self.query(f"VBS? '{script}'").strip()

    def vbs_set(self, path: str, value: Any) -> None:
        v = _quote_vbs_value(value)
        script = f"app.{path} = {v}"
        self.write(f"VBS '{script}'")

    # --------------------------
    # Human-friendly key resolver
    # --------------------------
    def to_vbs_path(self, key: str) -> str:
        # Direct static registry
        if key in PARAMS:
            return PARAMS[key]

        # channels.C1.ver_scale
        if key.startswith("channels."):
            _, ch, field = key.split(".", 2)
            if field not in CH_TEMPLATE:
                raise KeyError(f"Unknown channel field: {field} (key={key})")
            return CH_TEMPLATE[field].format(ch=ch)

        # trigger.levels.C1 / trigger.slopes.Ext / trigger.pattern_states.C1
        if key.startswith("trigger.levels."):
            src = key.split(".", 2)[2]
            return TRIG_SRC_TEMPLATE["level"].format(src=src)

        if key.startswith("trigger.slopes."):
            src = key.split(".", 2)[2]
            return TRIG_SRC_TEMPLATE["slope"].format(src=src)

        if key.startswith("trigger.pattern_states."):
            src = key.split(".", 2)[2]
            return TRIG_SRC_TEMPLATE["pattern_state"].format(src=src)

        # AUX OUT mode is family-dependent:
        #   WR/WS: app.Acquisition.AuxOutput.AuxMode
        #   WP:    app.Acquisition.AuxOutput.Mode
        if key == "aux_out.mode":
            fam = self.family
            if fam == "WRWS":
                return "Acquisition.AuxOutput.AuxMode"
            if fam == "WP":
                return "Acquisition.AuxOutput.Mode"
            # default guess: try WP-style first (more common outside WR/WS)
            return "Acquisition.AuxOutput.Mode"

        raise KeyError(f"Unknown parameter key: {key}")

    def set_param(self, key: str, value: Any) -> None:
        # Normalize AUX OUT mode enums
        if key == "aux_out.mode" and isinstance(value, str):
            value = _normalize_aux_mode(value)
        path = self.to_vbs_path(key)
        self.vbs_set(path, value)

    def get_param(self, key: str) -> str:
        path = self.to_vbs_path(key)
        return self.vbs_get(path)

    # Convenience helpers (optional)
    def set_aux_out_trigger_enabled(self) -> None:
        self.set_param("aux_out.mode", "TriggerEnabled")


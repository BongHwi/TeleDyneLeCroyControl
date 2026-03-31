#!/usr/bin/env python3
# apply_config.py
#
# Apply a human-friendly JSON config to a Teledyne LeCroy scope.
#
# Added:
#  - IP -> NAME mapping from config.json (scope_profile.scope_names)
#  - --runnumber to fill runXXXXXX (zero-padded 6 digits) into trace_title_prefix
#
# NEW (debug/robust):
#  - TRIG_MODE apply now verifies readback with TRIG_MODE?
#  - If mode_scpi is "NORMAL", also tries "NORM" (LeCroy common alias)
#  - Re-assert TRIG_MODE at end (helps detect "it got reset later" issues)
#  - Prints TRIG_SELECT? / TRIG_PATTERN? readback for trigger consistency
#
# Usage:
#   python3 apply_config.py --ip 192.168.0.100 --config config.json --runnumber 23

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from lecroy_visa import LeCroyVisa


# -------------------------
# Utilities
# -------------------------
def load_json(path: str) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sanitize_name(name: str) -> str:
    """
    Make a safe filename token. Keep [A-Za-z0-9._-], replace others with '_'.
    """
    name = name.strip()
    if not name:
        return "NONAME"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def resolve_scope_name(cfg: Dict[str, Any], ip: str) -> str:
    mapping = cfg.get("scope_profile", {}).get("scope_names", {})
    if isinstance(mapping, dict):
        v = mapping.get(ip)
        if v is not None and str(v).strip():
            return sanitize_name(str(v))
    return f"IP_{ip.replace('.', '_')}"


def format_runnumber(n: int) -> str:
    if n < 0:
        raise ValueError("runnumber must be non-negative")
    if n > 999999:
        raise ValueError("runnumber must be <= 999999")
    return f"{n:06d}"


def replace_run_in_prefix(prefix: str, run6: str) -> str:
    """
    Replace run portion in prefix:
      1) "XXXXXX" placeholder -> replaced
      2) "run" + 6 digits -> digits replaced
    """
    if "XXXXXX" in prefix:
        return prefix.replace("XXXXXX", run6)

    new, n = re.subn(r"run(\d{6})", f"run{run6}", prefix, count=1)
    if n == 1:
        return new

    raise RuntimeError(
        "trace_title_prefix must contain 'XXXXXX' (recommended) or match 'run' + 6 digits "
        f"to use --runnumber. Got: {prefix!r}"
    )


def _scope_set(scope: LeCroyVisa, human_key: str, value: Any) -> None:
    if hasattr(scope, "set_param"):
        scope.set_param(human_key, value)
        return
    if hasattr(scope, "to_vbs_path") and hasattr(scope, "vbs_set"):
        vbs_path = scope.to_vbs_path(human_key)
        scope.vbs_set(vbs_path, value)
        return
    raise AttributeError("LeCroyVisa lacks set_param() and (to_vbs_path + vbs_set).")


def _scope_write(scope: LeCroyVisa, scpi_cmd: str) -> None:
    if hasattr(scope, "write"):
        scope.write(scpi_cmd)
        return
    if hasattr(scope, "write_scpi"):
        scope.write_scpi(scpi_cmd)
        return
    raise AttributeError("LeCroyVisa lacks write()/write_scpi().")


def _scope_query(scope: LeCroyVisa, scpi_cmd: str) -> str:
    if hasattr(scope, "query"):
        return str(scope.query(scpi_cmd))
    if hasattr(scope, "query_scpi"):
        return str(scope.query_scpi(scpi_cmd))
    raise AttributeError("LeCroyVisa lacks query()/query_scpi().")


def set_with_policy(
    scope: LeCroyVisa,
    key: str,
    value: Any,
    dry_run: bool,
    stop_on_error: bool,
) -> Tuple[bool, Optional[str]]:
    try:
        if dry_run:
            print(f"[DRYRUN] set {key} = {value}")
            return True, None
        _scope_set(scope, key, value)
        print(f"[SET] {key} = {value}")
        return True, None
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        print(f"[ERR] {key} = {value} -> {msg}")
        if stop_on_error:
            raise
        return False, msg


def write_with_policy(
    scope: LeCroyVisa,
    cmd: str,
    dry_run: bool,
    stop_on_error: bool,
) -> Tuple[bool, Optional[str]]:
    try:
        if dry_run:
            print(f"[DRYRUN] write {cmd}")
            return True, None
        _scope_write(scope, cmd)
        print(f"[WRITE] {cmd}")
        return True, None
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        print(f"[ERR] write {cmd} -> {msg}")
        if stop_on_error:
            raise
        return False, msg


def query_with_policy(
    scope: LeCroyVisa,
    cmd: str,
    dry_run: bool,
    stop_on_error: bool,
) -> Tuple[Optional[str], Optional[str]]:
    try:
        if dry_run:
            print(f"[DRYRUN] query {cmd}")
            return None, None
        out = _scope_query(scope, cmd).strip()
        print(f"[READ] {cmd} -> {out}")
        return out, None
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        print(f"[ERR] query {cmd} -> {msg}")
        if stop_on_error:
            raise
        return None, msg


def set_trig_mode_verified(
    scope: LeCroyVisa,
    requested: str,
    dry_run: bool,
    stop_on_error: bool,
) -> None:
    """
    Robust TRIG_MODE setter + readback verification.

    - Tries the requested string.
    - If requested is "NORMAL", also tries "NORM" (common LeCroy alias).
    - Prints TRIG_MODE? readback.
    """
    req = str(requested).strip().upper()
    candidates = [req]
    if req == "NORMAL":
        candidates = ["NORMAL", "NORM"]

    last_err: Optional[str] = None
    for m in candidates:
        ok, err = write_with_policy(scope, f"TRIG_MODE {m}", dry_run, stop_on_error)
        if not ok:
            last_err = err
            continue

        rb, rb_err = query_with_policy(scope, "TRIG_MODE?", dry_run, stop_on_error)
        if rb_err is not None:
            last_err = rb_err
            continue

        # If dry_run, rb is None; accept
        if rb is None:
            print(f"[TRIG_MODE] (dry-run) set={m}")
            return

        # Accept if readback contains our mode token (AUTO/NORM/SINGLE/STOP/etc.)
        # LeCroy sometimes returns "NORM" rather than "NORMAL".
        rb_u = rb.strip().upper()
        want_tokens = {m}
        if m == "NORMAL":
            want_tokens.add("NORM")
        if m == "NORM":
            want_tokens.add("NORMAL")

        if any(tok in rb_u for tok in want_tokens):
            print(f"[TRIG_MODE] set={m} readback={rb}")
            return

        # Mismatch: continue trying other candidates
        last_err = f"readback mismatch (set {m}, got {rb})"

    # If we reach here, all candidates failed or mismatched
    msg = f"Failed to set TRIG_MODE to {requested!r}. Last error: {last_err}"
    print(f"[WARN] {msg}")
    if stop_on_error:
        raise RuntimeError(msg)


def print_trigger_readbacks(scope: LeCroyVisa, dry_run: bool, stop_on_error: bool) -> None:
    """
    Helpful consistency readbacks after applying trigger-related settings.
    """
    print("\n=== Trigger readback (SCPI) ===")
    query_with_policy(scope, "TRIG_MODE?", dry_run, stop_on_error)
    query_with_policy(scope, "TRIG_SELECT?", dry_run, stop_on_error)
    query_with_policy(scope, "TRIG_PATTERN?", dry_run, stop_on_error)


# -------------------------
# Apply config (human-friendly)
# -------------------------
def apply_human_config(
    scope: LeCroyVisa,
    cfg: Dict[str, Any],
    scope_name: str,
    runnumber_str: Optional[str] = None,
) -> None:
    dry_run = bool(cfg.get("dry_run", False))
    stop_on_error = bool(cfg.get("stop_on_error", False))

    idn = scope.idn() if hasattr(scope, "idn") else "(idn unavailable)"
    print(f"Connected: {idn}")
    print(f"Name     : {scope_name}")
    print(f"Dry-run  : {dry_run}   Stop-on-error: {stop_on_error}")

    # Keep track if mode_scpi was requested; we re-assert at end
    requested_trig_mode_scpi: Optional[str] = None

    # -------------------------
    # horizontal
    # -------------------------
    horizontal = cfg.get("horizontal", {})
    if horizontal:
        print("\n=== Apply: horizontal ===")
        for k in [
            "sample_mode",
            "num_segments",
            "sequence_timeout_enable",
            "sequence_timeout",
            "hor_scale",
            "hor_offset",
        ]:
            if k in horizontal:
                set_with_policy(scope, f"horizontal.{k}", horizontal[k], dry_run, stop_on_error)

    # -------------------------
    # channels
    # -------------------------
    channels = cfg.get("channels", {})
    if channels:
        print("\n=== Apply: channels ===")
        for ch, params in channels.items():
            if not isinstance(params, dict):
                continue
            for field, val in params.items():
                set_with_policy(scope, f"channels.{ch}.{field}", val, dry_run, stop_on_error)

    # -------------------------
    # trigger
    # -------------------------
    trigger = cfg.get("trigger", {})
    if trigger:
        print("\n=== Apply: trigger ===")
        if "type" in trigger:
            set_with_policy(scope, "trigger.type", trigger["type"], dry_run, stop_on_error)
        if "source" in trigger:
            set_with_policy(scope, "trigger.source", trigger["source"], dry_run, stop_on_error)

        levels = trigger.get("levels", {})
        if isinstance(levels, dict):
            for src, v in levels.items():
                set_with_policy(scope, f"trigger.levels.{src}", v, dry_run, stop_on_error)

        slopes = trigger.get("slopes", {})
        if isinstance(slopes, dict):
            for src, v in slopes.items():
                set_with_policy(scope, f"trigger.slopes.{src}", v, dry_run, stop_on_error)

        # Logic/Pattern extras (optional)
        if "pattern_type" in trigger:
            set_with_policy(scope, "trigger.pattern_type", trigger["pattern_type"], dry_run, stop_on_error)

        pattern_states = trigger.get("pattern_states", {})
        if isinstance(pattern_states, dict):
            for src, st in pattern_states.items():
                set_with_policy(scope, f"trigger.pattern_states.{src}", st, dry_run, stop_on_error)

        # SCPI trigger mode (optional) with verification
        mode_scpi = trigger.get("mode_scpi", None)
        if mode_scpi:
            requested_trig_mode_scpi = str(mode_scpi).strip()
            set_trig_mode_verified(scope, requested_trig_mode_scpi, dry_run, stop_on_error)

        # Always print trigger readbacks after applying trigger section
        print_trigger_readbacks(scope, dry_run, stop_on_error)

    # -------------------------
    # AUX OUT
    # -------------------------
    aux = cfg.get("aux_out", {})
    if aux:
        print("\n=== Apply: AUX OUT ===")
        if "mode" in aux:
            set_with_policy(scope, "aux_out.mode", aux["mode"], dry_run, stop_on_error)
        if "aux_in_coupling" in aux:
            set_with_policy(scope, "aux_out.aux_in_coupling", aux["aux_in_coupling"], dry_run, stop_on_error)

    # -------------------------
    # save_waveforms
    # -------------------------
    sw = cfg.get("save_waveforms", {})
    if sw and bool(sw.get("enabled", False)):
        print("\n=== Apply: save_waveforms ===")

        # Resolve runnumber into trace_title_prefix
        if runnumber_str is not None:
            prefix = str(sw.get("trace_title_prefix", ""))
            new_prefix = replace_run_in_prefix(prefix, runnumber_str)
            sw["trace_title_prefix"] = new_prefix
            print(f"[INFO] resolved trace_title_prefix: {prefix!r} -> {new_prefix!r}")

        # Apply mapped save keys
        if "save_to" in sw:
            set_with_policy(scope, "save.save_to", sw["save_to"], dry_run, stop_on_error)
        if "waveform_dir" in sw:
            set_with_policy(scope, "save.waveform_dir", sw["waveform_dir"], dry_run, stop_on_error)
        if "wave_format" in sw:
            set_with_policy(scope, "save.wave_format", sw["wave_format"], dry_run, stop_on_error)
        if "trace_title_prefix" in sw:
            set_with_policy(scope, "save.trace_title", sw["trace_title_prefix"], dry_run, stop_on_error)

        if "sources" in sw:
            print(f"[INFO] save_waveforms.sources = {sw['sources']}")

    # -------------------------
    # Final: re-assert TRIG_MODE if requested (detect resets)
    # -------------------------
    if requested_trig_mode_scpi:
        print("\n=== Final verify: TRIG_MODE re-assert ===")
        set_trig_mode_verified(scope, requested_trig_mode_scpi, dry_run, stop_on_error)
        print_trigger_readbacks(scope, dry_run, stop_on_error)

    print("\nDone.")


# -------------------------
# Main
# -------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Apply LeCroy scope settings from a human-friendly JSON config.")
    ap.add_argument("--ip", required=True, help="Scope IP address")
    ap.add_argument("--backend", default="@py", help="pyvisa backend (default: @py)")
    ap.add_argument("--timeout", type=float, default=5.0, help="VISA timeout seconds")
    ap.add_argument("--config", default="config.json", help="Config JSON path")

    ap.add_argument(
        "--runnumber",
        type=int,
        default=None,
        help="Run number (int). Replaces 'XXXXXX' or 'run'+6digits in trace_title_prefix with zero-padded 6 digits.",
    )

    args = ap.parse_args()
    cfg = load_json(args.config)

    scope_name = resolve_scope_name(cfg, args.ip)

    runnumber_str = None
    if args.runnumber is not None:
        runnumber_str = format_runnumber(args.runnumber)

    scope = LeCroyVisa(address=args.ip, visa_backend=args.backend, timeout_s=args.timeout)
    scope.connect()
    try:
        apply_human_config(scope, cfg, scope_name=scope_name, runnumber_str=runnumber_str)
    finally:
        scope.close()


if __name__ == "__main__":
    main()

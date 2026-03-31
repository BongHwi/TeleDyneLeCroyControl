#!/usr/bin/env python3
# acquire_data.py
#
# Realtime acquisition loop:
#  - Trigger wait via VBS Acquire(timeoutSeconds, forceTriggerOnTimeout)
#  - Save waveform traces to scope memory (M1..M4 rotating) with TraceTitle
#  - Optionally (future) copy from scope to PC (not implemented here)
#
# Fix:
#  - Extend VISA timeout while waiting for VBS Acquire to return,
#    otherwise PyVISA default timeout (often 5s) causes VI_ERROR_TMO.

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from lecroy_visa import LeCroyVisa


def load_json(path: str) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sanitize_name(name: str) -> str:
    name = str(name).strip()
    if not name:
        return "NONAME"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def resolve_scope_name(cfg: Dict[str, Any], ip: str) -> str:
    mapping = cfg.get("scope_profile", {}).get("scope_names", {})
    if isinstance(mapping, dict) and ip in mapping and str(mapping[ip]).strip():
        return sanitize_name(mapping[ip])
    return f"IP_{ip.replace('.', '_')}"


def run_tag(run_number: int) -> str:
    return f"run{int(run_number):06d}"


def get_sources(cfg: Dict[str, Any], channels_max: int) -> List[str]:
    # Use save_waveforms.sources if present; else default C1..C{channels_max}
    sw = cfg.get("save_waveforms", {}) or {}
    srcs = sw.get("sources")
    if isinstance(srcs, list) and srcs:
        return [str(s) for s in srcs]
    return [f"C{i}" for i in range(1, channels_max + 1)]


def infer_channels_max(scope: LeCroyVisa, cfg: Dict[str, Any]) -> int:
    # Prefer model auto
    m = (getattr(scope, "model", "") or "").upper()
    if m.startswith("WP"):
        return 4
    if m.startswith("WR"):
        return 8
    # fallback config
    return int(cfg.get("scope_profile", {}).get("channels_max", 8))


def vbs_acquire(scope: LeCroyVisa, timeout_s: float, force_on_timeout: bool) -> bool:
    """
    Calls:
      app.Acquisition.Acquire(timeoutSeconds, forceTriggerOnTimeout)
    Returns True if we can interpret as "triggered", else False.
    Note: Some firmware returns empty/None/False; we treat that as not-triggered.
    """
    vbs = f"app.Acquisition.Acquire({float(timeout_s)}, {int(bool(force_on_timeout))})"

    # IMPORTANT: VBS Acquire blocks up to timeout_s, so VISA timeout must exceed it.
    with scope.temp_timeout(float(timeout_s) + 5.0):
        ret = scope.vbs_call(vbs)

    if ret is None:
        return False
    s = str(ret).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    return False


def ensure_trace_title_prefix(cfg: Dict[str, Any], scope_model: str, scope_name: str, run: str) -> str:
    sw = cfg.get("save_waveforms", {}) or {}
    prefix = sw.get("trace_title_prefix")
    if prefix is None:
        prefix = ""
    prefix = str(prefix)

    # We always build titles like:
    #   <MODEL>_<NAME>_<runXXXXXX>_<SRC>_shot000001
    # so "trace_title_prefix" is not strictly required.
    return prefix


def save_trace_to_memory(scope: LeCroyVisa, src: str, mem: str, title: str) -> None:
    """
    Save a displayed trace to memory slot (M1..M4) and set TraceTitle.
    This uses your existing LeCroyVisa helpers (expected):
      - scope.save_to_memory(source, mem, trace_title)
    If your lecroy_visa.py uses different method names, adjust here.
    """
    # If your LeCroyVisa has a high-level helper, use it:
    if hasattr(scope, "save_trace_to_memory"):
        scope.save_trace_to_memory(src, mem, title)
        return

    # Otherwise do it via VBS (generic MAUI style):
    #   app.SaveRecall.SaveTo = "Memory"
    #   app.SaveRecall.WaveformDir = "D:\Waveforms\"
    #   app.SaveRecall.TraceTitle = "..."
    #   app.SaveRecall.SaveSource = "C1"
    #   app.SaveRecall.SaveWaveform
    scope.vbs_set("app.SaveRecall.SaveTo", "Memory")
    scope.vbs_set("app.SaveRecall.TraceTitle", title)
    scope.vbs_set("app.SaveRecall.SaveSource", src)
    # Some models allow selecting destination memory trace explicitly:
    # We'll try best-effort:
    try:
        scope.vbs_set("app.SaveRecall.SaveDestination", mem)
    except Exception:
        pass
    scope.vbs_call("app.SaveRecall.SaveWaveform")


def main() -> None:
    ap = argparse.ArgumentParser(description="Acquire waveform shots (Realtime) and save to scope memory.")
    ap.add_argument("--ip", required=True, help="Scope IP address")
    ap.add_argument("--backend", default="@py", help="pyvisa backend (default: @py)")

    ap.add_argument("--config", default="config.json", help="Config JSON path")
    ap.add_argument("--run", type=int, required=True, help="Run number (will become runXXXXXX)")
    ap.add_argument("--n", type=int, default=10, help="Number of shots")

    ap.add_argument("--acq-timeout", type=float, default=10.0, help="Acquire timeoutSeconds (VBS Acquire)")
    ap.add_argument("--force-on-timeout", action="store_true", help="Force trigger when timeout occurs")

    ap.add_argument("--visa-timeout", type=float, default=5.0, help="Base VISA timeout seconds (non-Acquire ops)")
    ap.add_argument("--debug", action="store_true", help="Verbose prints")

    args = ap.parse_args()

    cfg = load_json(args.config)

    scope = LeCroyVisa(address=args.ip, visa_backend=args.backend, timeout_s=float(args.visa_timeout))
    scope.connect()

    scopename = resolve_scope_name(cfg, args.ip)
    channels_max = infer_channels_max(scope, cfg)
    sources = get_sources(cfg, channels_max)

    run = run_tag(args.run)

    print(f"Connected: {scope.idn()}")
    print(f"ScopeName: {scopename}  Model: {scope.model}")
    print(f"Run     : {run}")
    print(f"Sources : {sources}")
    print(f"Acquire : n={args.n}, acq_timeout={args.acq_timeout}s, force_on_timeout={bool(args.force_on_timeout)}")

    # Memory slot rotation for saved traces (M1..M4)
    mem_slots = ["M1", "M2", "M3", "M4"]

    triggered = 0
    timed_out = 0

    for i in range(1, int(args.n) + 1):
        # wait for trigger
        try:
            t = vbs_acquire(scope, float(args.acq_timeout), bool(args.force_on_timeout))
        except Exception as e:
            # Don't crash the run; report and break
            print(f"[ERROR] Acquire failed at shot={i}: {type(e).__name__}: {e}")
            break

        if t:
            triggered += 1
        else:
            timed_out += 1

        if args.debug:
            print(f"[ACQ] shot={i}/{args.n} triggerDetected={t}")

        # Save each channel to memory slot (rotate)
        mem = mem_slots[(i - 1) % len(mem_slots)]
        for src in sources:
            title = f"{scope.model}_{scopename}_{run}_{src}_shot{i:06d}"
            try:
                print(f"[SAVE:SCOPE] {src} -> {mem} (TraceTitle={title})")
                save_trace_to_memory(scope, src, mem, title)
            except Exception as e:
                print(f"[SKIP] {src}: {type(e).__name__}: {e}")

    print(f"\nDone. triggered={triggered}, timed_out={timed_out}")
    scope.close()


if __name__ == "__main__":
    main()
    

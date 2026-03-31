#!/usr/bin/env python3
# scope_snapshot.py
#
# Snapshot LeCroy scope settings:
#  - Print human-readable summary (optional)
#  - Dump JSON snapshot file (optional)
#  - Show missing/unsupported keys (optional)
#  - Diff against a previous snapshot JSON (optional)
#
# Added:
#  - IP -> NAME mapping from config.json (scope_profile.scope_names)
#  - Output filename: snapshots/<MODEL>_<NAME>_<runXXXXXX>.json (fallback to timestamp)
#
# Status (diagnostic) support:
#  - SCPI state probes (TRIG_MODE?, TRIG_SELECT?, TRIG_PATTERN?, ...)
#  - Optional VBS Acquire probe:
#       app.Acquisition.Acquire(timeoutSeconds, forceTriggerOnTimeout)
#
# NEW:
#  - Auto channel count selection by model:
#       WP... (WavePro)    -> 4ch
#       WR... (WaveRunner) -> 8ch
#    CLI --channels-max overrides everything.
#
from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lecroy_visa import LeCroyVisa

INVALID_MARKERS = (
    "Object doesn't support this property or method",
    "Invalid procedure call or argument",
)


# -----------------------------
# Helpers: JSON I/O
# -----------------------------
def load_json(path: str) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def sanitize_name(name: str) -> str:
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


def extract_run_tag_from_title(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    m = re.search(r"(run\d{6})", str(title))
    if m:
        return m.group(1)
    return None


# -----------------------------
# Channel count inference
# -----------------------------
def infer_channels_max_from_model(model: str, family: str) -> Optional[int]:
    m = (model or "").upper().strip()
    f = (family or "").upper().strip()

    # Typical model examples:
    #   WP804HD  -> WavePro, 4ch
    #   WR8208HD -> WaveRunner, 8ch
    if m.startswith("WP"):
        return 4
    if m.startswith("WR"):
        return 8

    # Fallback: family string if populated
    if "WAVEPRO" in f:
        return 4
    if "WAVERUNNER" in f:
        return 8

    return None


def get_channels_max(
    cfg: Dict[str, Any],
    override: Optional[int],
    scope: Optional[LeCroyVisa] = None,
) -> int:
    # Highest priority: CLI override
    if override is not None:
        return int(override)

    # Next: infer from connected scope model (best)
    if scope is not None:
        inferred = infer_channels_max_from_model(getattr(scope, "model", ""), getattr(scope, "family", ""))
        if inferred is not None:
            return int(inferred)

    # Next: config default
    v = cfg.get("scope_profile", {}).get("channels_max", None)
    if v is not None:
        return int(v)

    # Final fallback
    return 8


# -----------------------------
# Helpers: VBS read with robust unsupported detection
# -----------------------------
def is_unsupported(raw: Optional[str]) -> Tuple[bool, str]:
    if raw is None:
        return True, "no return"
    r = str(raw).strip()
    if r == "":
        return True, "empty return"
    for m in INVALID_MARKERS:
        if m in r:
            return True, r
    return False, ""


def safe_read_key(scope: LeCroyVisa, key: str) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    try:
        vbs_path = scope.to_vbs_path(key)
    except Exception as e:
        return None, {"key": key, "path": "(unresolved)", "reason": f"{type(e).__name__}: {e}"}

    try:
        raw = scope.vbs_get(vbs_path)
        bad, reason = is_unsupported(raw)
        if bad:
            return None, {"key": key, "path": vbs_path, "reason": reason}
        return str(raw).strip(), None
    except Exception as e:
        return None, {"key": key, "path": vbs_path, "reason": f"{type(e).__name__}: {e}"}


def safe_scpi_query(scope: LeCroyVisa, cmd: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        v = scope.query(cmd)
        if v is None:
            return None, "no return"
        s = str(v).strip()
        if s == "":
            return None, "empty return"
        return s, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def safe_vbs_get_raw(scope: LeCroyVisa, vbs_path: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        raw = scope.vbs_get(vbs_path)
        bad, reason = is_unsupported(raw)
        if bad:
            return None, reason
        return str(raw).strip(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# -----------------------------
# Snapshot model
# -----------------------------
@dataclass
class Snapshot:
    idn: str
    model: str
    family: str
    ip: str
    scope_name: str
    run_tag: Optional[str]
    timestamp_local: str
    readback_human: Dict[str, Any]
    readback_raw: Dict[str, Any]
    missing: List[Dict[str, str]]

    def to_dict(self, include_raw: bool = True) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "idn": self.idn,
            "model": self.model,
            "family": self.family,
            "ip": self.ip,
            "scope_name": self.scope_name,
            "run_tag": self.run_tag,
            "timestamp_local": self.timestamp_local,
            "readback_human": self.readback_human,
            "missing": self.missing,
        }
        if include_raw:
            d["readback_raw"] = self.readback_raw
        return d


# -----------------------------
# Build target key list
# -----------------------------
def build_keys(cfg: Dict[str, Any], channels_max: int, only: str) -> List[str]:
    only = only.lower()
    include_all = (only == "all")

    keys: List[str] = []

    # Horizontal
    if include_all or only == "horizontal":
        keys += [
            "horizontal.sample_mode",
            "horizontal.num_segments",
            "horizontal.sequence_timeout_enable",
            "horizontal.sequence_timeout",
            "horizontal.hor_scale",
            "horizontal.hor_offset",
            "horizontal.num_points",
            "horizontal.time_per_point",
        ]

    # Trigger
    if include_all or only == "trigger":
        keys += [
            "trigger.type",
            "trigger.source",
            "trigger.pattern_type",
            "trigger.pattern_level",
        ]
        for src in [f"C{i}" for i in range(1, channels_max + 1)] + ["Ext"]:
            keys.append(f"trigger.levels.{src}")
            keys.append(f"trigger.slopes.{src}")
            keys.append(f"trigger.pattern_states.{src}")

    # Channels
    if include_all or only == "channels":
        ch_fields = ["view", "coupling", "scale", "offset", "invert", "bandwidth_limit", "deskew", "units"]
        for i in range(1, channels_max + 1):
            ch = f"C{i}"
            for f in ch_fields:
                keys.append(f"channels.{ch}.{f}")

    # Save waveform settings (properties only)
    if include_all or only in ("save", "save_waveforms"):
        keys += [
            "save.save_to",
            "save.waveform_dir",
            "save.wave_format",
            "save.trace_title",
            "save.save_source",
        ]

    # AUX OUT
    if include_all or only in ("aux", "aux_out"):
        keys += [
            "aux_out.mode",
            "aux_out.aux_in_coupling",
        ]

    return keys


# -----------------------------
# Status collection
# -----------------------------
def collect_status(
    scope: LeCroyVisa,
    do_probe_acquire: bool,
    probe_timeout_s: float,
    probe_force: bool,
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    status: Dict[str, Any] = {}
    miss: List[Dict[str, str]] = []

    # SCPI probes (read-only)
    scpi_cmds = [
        "TRIG_MODE?",
        "TRIG_STATE?",
        "TRIG_SELECT?",
        "TRIG_PATTERN?",
        "COMM_HEADER?",
        "COMM_FORMAT?",
        "COMM_ORDER?",
    ]
    for cmd in scpi_cmds:
        v, err = safe_scpi_query(scope, cmd)
        key = f"status.scpi.{cmd.replace('?', '').lower()}"
        if v is not None:
            status[key] = v
        else:
            miss.append({"key": key, "path": cmd, "reason": err or "unavailable"})

    # VBS probes (best-effort)
    vbs_candidates = [
        "app.Acquisition.State",
        "app.Acquisition.RunState",
        "app.Acquisition.IsRunning",
        "app.Acquisition.TriggerState",
        "app.Acquisition.Horizontal.SampleMode",
        "app.Acquisition.Horizontal.NumSegments",
        "app.Acquisition.Horizontal.SequenceTimeout",
        "app.Acquisition.Horizontal.SequenceTimeoutEnable",
    ]
    for path in vbs_candidates:
        v, err = safe_vbs_get_raw(scope, path)
        key = f"status.vbs.{path.replace('app.', '').lower()}"
        if v is not None:
            status[key] = v
        else:
            miss.append({"key": key, "path": path, "reason": err or "unavailable"})

    # Optional Acquire probe (side effects)
    if do_probe_acquire:
        vbs = f"app.Acquisition.Acquire({float(probe_timeout_s)}, {int(bool(probe_force))})"
        t0 = time.time()
        try:
            ret = scope.vbs_call(vbs)
            dt = time.time() - t0
            status["status.probe.acquire.vbs"] = vbs
            status["status.probe.acquire.dt_seconds"] = round(dt, 6)
            status["status.probe.acquire.return_raw"] = "" if ret is None else str(ret).strip()
        except Exception as e:
            dt = time.time() - t0
            status["status.probe.acquire.vbs"] = vbs
            status["status.probe.acquire.dt_seconds"] = round(dt, 6)
            miss.append({"key": "status.probe.acquire", "path": vbs, "reason": f"{type(e).__name__}: {e}"})

    return status, miss


# -----------------------------
# Pretty printing helpers
# -----------------------------
def _fmt_kv(k: str, v: Any, pad: int) -> str:
    s = "" if v is None else str(v)
    return f"{k:<{pad}} : {s}"


def print_header(scope: LeCroyVisa, ip: str, scope_name: str, channels_max: int) -> None:
    print(f"Connected: {scope.idn()}")
    print(f"Model    : {scope.model}   Family: {scope.family}")
    print(f"IP       : {ip}")
    print(f"Name     : {scope_name}")
    print(f"Channels : {channels_max} (auto; use --channels-max to override)")


def print_group(title: str, items: List[Tuple[str, Any]]) -> None:
    if not items:
        return
    print(f"\n[{title}]")
    pad = max(len(k) for k, _ in items)
    for k, v in items:
        print(_fmt_kv(k, v, pad))


def _norm_trigger_type(raw: Any) -> str:
    s = ("" if raw is None else str(raw)).strip().lower()
    if s == "edge":
        return "EDGE"
    if s in ("logic", "pattern"):
        return "LOGIC"
    return "OTHER"


def group_items(readback_human: Dict[str, Any], only: str, trigger_view: str) -> Dict[str, List[Tuple[str, Any]]]:
    only = only.lower()
    trigger_view = trigger_view.lower()

    groups: Dict[str, List[Tuple[str, Any]]] = {
        "Status": [],
        "Trigger": [],
        "Horizontal": [],
        "Channels": [],
        "Save": [],
        "AUX OUT": [],
    }

    def add(group: str, key: str, label: str) -> None:
        if key in readback_human:
            groups[group].append((label, readback_human.get(key)))

    # Status
    for k in sorted(readback_human.keys()):
        if k.startswith("status."):
            groups["Status"].append((k.replace("status.", ""), readback_human.get(k)))

    # Horizontal
    if only in ("all", "horizontal"):
        add("Horizontal", "horizontal.sample_mode", "sample_mode")
        add("Horizontal", "horizontal.num_segments", "num_segments")
        add("Horizontal", "horizontal.sequence_timeout_enable", "sequence_timeout_enable")
        add("Horizontal", "horizontal.sequence_timeout", "sequence_timeout")
        add("Horizontal", "horizontal.hor_scale", "hor_scale")
        add("Horizontal", "horizontal.hor_offset", "hor_offset")
        add("Horizontal", "horizontal.num_points", "num_points")
        add("Horizontal", "horizontal.time_per_point", "time_per_point")

    # Trigger
    if only in ("all", "trigger"):
        t_raw = readback_human.get("trigger.type")
        t_norm = _norm_trigger_type(t_raw)
        src = readback_human.get("trigger.source")

        if trigger_view == "full":
            add("Trigger", "trigger.type", "type")
            add("Trigger", "trigger.source", "source")
            add("Trigger", "trigger.pattern_type", "pattern_type")
            add("Trigger", "trigger.pattern_level", "pattern_level")

            for k in sorted(readback_human.keys()):
                if k.startswith("trigger.levels."):
                    add("Trigger", k, k.replace("trigger.levels.", "level."))
            for k in sorted(readback_human.keys()):
                if k.startswith("trigger.slopes."):
                    add("Trigger", k, k.replace("trigger.slopes.", "slope."))
            for k in sorted(readback_human.keys()):
                if k.startswith("trigger.pattern_states."):
                    add("Trigger", k, k.replace("trigger.pattern_states.", "pstate."))

        else:
            # SMART:
            # - EDGE: show only source-related
            # - LOGIC: show all (including Don't Care) for safety
            add("Trigger", "trigger.type", "type")

            if t_norm == "EDGE":
                add("Trigger", "trigger.source", "source")
                if src is not None:
                    k_level = f"trigger.levels.{src}"
                    k_slope = f"trigger.slopes.{src}"
                    if k_level in readback_human:
                        add("Trigger", k_level, f"level.{src}")
                    if k_slope in readback_human:
                        add("Trigger", k_slope, f"slope.{src}")

            elif t_norm == "LOGIC":
                add("Trigger", "trigger.pattern_type", "pattern_type")
                add("Trigger", "trigger.pattern_level", "pattern_level")
                add("Trigger", "trigger.source", "source")

                for k in sorted(readback_human.keys()):
                    if k.startswith("trigger.pattern_states."):
                        add("Trigger", k, k.replace("trigger.pattern_states.", "pstate."))
                for k in sorted(readback_human.keys()):
                    if k.startswith("trigger.levels."):
                        add("Trigger", k, k.replace("trigger.levels.", "level."))
                for k in sorted(readback_human.keys()):
                    if k.startswith("trigger.slopes."):
                        add("Trigger", k, k.replace("trigger.slopes.", "slope."))

            else:
                add("Trigger", "trigger.source", "source")
                add("Trigger", "trigger.pattern_type", "pattern_type")
                add("Trigger", "trigger.pattern_level", "pattern_level")
                for k in sorted(readback_human.keys()):
                    if k.startswith("trigger.levels."):
                        add("Trigger", k, k.replace("trigger.levels.", "level."))
                for k in sorted(readback_human.keys()):
                    if k.startswith("trigger.slopes."):
                        add("Trigger", k, k.replace("trigger.slopes.", "slope."))
                for k in sorted(readback_human.keys()):
                    if k.startswith("trigger.pattern_states."):
                        add("Trigger", k, k.replace("trigger.pattern_states.", "pstate."))

    # Channels
    if only in ("all", "channels"):
        for k in sorted(readback_human.keys()):
            if k.startswith("channels."):
                groups["Channels"].append((k.replace("channels.", ""), readback_human.get(k)))

    # Save
    if only in ("all", "save", "save_waveforms"):
        add("Save", "save.save_to", "save_to")
        add("Save", "save.waveform_dir", "waveform_dir")
        add("Save", "save.wave_format", "wave_format")
        add("Save", "save.trace_title", "trace_title")
        add("Save", "save.save_source", "save_source")

    # AUX OUT
    if only in ("all", "aux", "aux_out"):
        add("AUX OUT", "aux_out.mode", "mode")
        add("AUX OUT", "aux_out.aux_in_coupling", "aux_in_coupling")

    return groups


def print_missing(missing: List[Dict[str, str]], show_all: bool, limit: int = 50) -> None:
    if not missing:
        print("\n[Missing] none")
        return

    print(f"\n[Missing] {len(missing)} key(s) unavailable/failed")
    items = missing if show_all else missing[:limit]
    for m in items:
        print(f"  - {m['key']} ({m['path']}): {m['reason']}")
    if (not show_all) and len(missing) > limit:
        print(f"  ... ({len(missing)-limit} more; use --show-missing for all)")


# -----------------------------
# Diff
# -----------------------------
def diff_snapshots(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    old_rb = old.get("readback_human", {}) or {}
    new_rb = new.get("readback_human", {}) or {}

    added: Dict[str, Any] = {}
    removed: Dict[str, Any] = {}
    changed: Dict[str, Dict[str, Any]] = {}

    old_keys = set(old_rb.keys())
    new_keys = set(new_rb.keys())

    for k in sorted(new_keys - old_keys):
        added[k] = new_rb.get(k)

    for k in sorted(old_keys - new_keys):
        removed[k] = old_rb.get(k)

    for k in sorted(old_keys & new_keys):
        if old_rb.get(k) != new_rb.get(k):
            changed[k] = {"old": old_rb.get(k), "new": new_rb.get(k)}

    return {"added": added, "removed": removed, "changed": changed}


def print_diff(d: Dict[str, Any]) -> None:
    added = d["added"]
    removed = d["removed"]
    changed = d["changed"]

    print("\n=== Diff (readback_human) ===")
    if not added and not removed and not changed:
        print("No differences.")
        return

    if added:
        print(f"\n[Added] {len(added)}")
        for k, v in added.items():
            print(f"  + {k} : {v}")

    if removed:
        print(f"\n[Removed] {len(removed)}")
        for k, v in removed.items():
            print(f"  - {k} : {v}")

    if changed:
        print(f"\n[Changed] {len(changed)}")
        for k, vv in changed.items():
            print(f"  * {k}")
            print(f"      old: {vv['old']}")
            print(f"      new: {vv['new']}")


# -----------------------------
# Output path builder
# -----------------------------
def build_out_path(out_arg: Optional[str], model: str, scope_name: str, run_tag: Optional[str]) -> Optional[Path]:
    if out_arg is None:
        return None

    p = Path(out_arg)

    if p.suffix.lower() == ".json":
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    p.mkdir(parents=True, exist_ok=True)

    if run_tag:
        fname = f"{model}_{scope_name}_{run_tag}.json"
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"{model}_{scope_name}_{ts}.json"

    return p / fname


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Snapshot LeCroy scope settings (print + JSON dump + diff).")
    ap.add_argument("--ip", required=True, help="Scope IP address")
    ap.add_argument("--backend", default="@py", help="pyvisa backend (default: @py)")
    ap.add_argument("--timeout", type=float, default=5.0, help="VISA timeout seconds")

    ap.add_argument("--config", default="config.json", help="Config JSON path (used for IP->NAME + channels_max)")
    ap.add_argument("--channels-max", type=int, default=None, help="Override channel count (e.g. 4 or 8)")

    ap.add_argument(
        "--only",
        default="all",
        choices=["all", "trigger", "channels", "horizontal", "save", "save_waveforms", "aux", "aux_out"],
        help="Limit snapshot to a category",
    )

    ap.add_argument(
        "--trigger-view",
        default="smart",
        choices=["smart", "full"],
        help="Trigger print style: smart (Edge minimal, Logic full) or full (always all trigger keys)",
    )

    ap.add_argument("--status", dest="do_status", action="store_true", help="Collect SCPI/VBS status (recommended)")
    ap.add_argument("--no-status", dest="do_status", action="store_false", help="Do not collect status")
    ap.set_defaults(do_status=True)

    ap.add_argument("--probe-acquire", action="store_true", help="(Side effects) Call VBS Acquire once")
    ap.add_argument("--probe-timeout", type=float, default=1.0, help="Probe Acquire timeoutSeconds")
    ap.add_argument("--probe-force", action="store_true", help="Probe Acquire forceTriggerOnTimeout=1")

    ap.add_argument("--print", dest="do_print", action="store_true", help="Print human-readable snapshot")
    ap.add_argument("--no-print", dest="do_print", action="store_false", help="Do not print snapshot")
    ap.set_defaults(do_print=True)

    ap.add_argument("--json", dest="do_json", action="store_true", help="Write JSON snapshot")
    ap.add_argument("--no-json", dest="do_json", action="store_false", help="Do not write JSON snapshot")
    ap.set_defaults(do_json=True)

    ap.add_argument("--out", default="snapshots/", help="Output directory or explicit .json file path")

    ap.add_argument("--include-raw", action="store_true", help="Include readback_raw in JSON (bigger)")
    ap.add_argument("--no-raw", action="store_true", help="Exclude readback_raw from JSON (smaller)")

    ap.add_argument("--show-missing", action="store_true", help="Print all missing/unsupported keys")
    ap.add_argument("--strict", action="store_true", help="Exit with non-zero status if any missing keys")
    ap.add_argument("--diff", default=None, help="Diff against a previous snapshot JSON file")

    args = ap.parse_args()

    cfg = load_json(args.config)
    scope_name = resolve_scope_name(cfg, args.ip)

    scope = LeCroyVisa(address=args.ip, visa_backend=args.backend, timeout_s=args.timeout)
    scope.connect()

    # Decide channel count AFTER connect (auto by model/family)
    channels_max = get_channels_max(cfg, args.channels_max, scope=scope)

    keys = build_keys(cfg, channels_max=channels_max, only=args.only)

    readback_human: Dict[str, Any] = {}
    readback_raw: Dict[str, Any] = {}
    missing: List[Dict[str, str]] = []

    if args.do_status:
        status_dict, status_missing = collect_status(
            scope,
            do_probe_acquire=args.probe_acquire,
            probe_timeout_s=args.probe_timeout,
            probe_force=args.probe_force,
        )
        readback_human.update(status_dict)
        missing.extend(status_missing)

    for k in keys:
        val, miss = safe_read_key(scope, k)
        readback_human[k] = val

        try:
            vbs_path = scope.to_vbs_path(k)
            readback_raw[vbs_path] = val
        except Exception:
            pass

        if miss is not None:
            missing.append(miss)

    run_tag = extract_run_tag_from_title(readback_human.get("save.trace_title"))

    snap = Snapshot(
        idn=scope.idn(),
        model=scope.model,
        family=scope.family,
        ip=args.ip,
        scope_name=scope_name,
        run_tag=run_tag,
        timestamp_local=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        readback_human=readback_human,
        readback_raw=readback_raw,
        missing=missing,
    )

    if args.do_print:
        print_header(scope, args.ip, scope_name, channels_max=channels_max)
        if run_tag:
            print(f"Run      : {run_tag}")

        groups = group_items(readback_human=readback_human, only=args.only, trigger_view=args.trigger_view)
        for title in ["Status", "Trigger", "Horizontal", "AUX OUT", "Save", "Channels"]:
            print_group(title, groups.get(title, []))

        print_missing(missing, show_all=args.show_missing)

        if args.diff:
            old = load_json(args.diff)
            d = diff_snapshots(old, snap.to_dict(include_raw=True))
            print_diff(d)

    if args.do_json:
        include_raw = args.include_raw and (not args.no_raw)
        if args.no_raw:
            include_raw = False

        out_path = build_out_path(args.out, model=scope.model, scope_name=scope_name, run_tag=run_tag)
        if out_path is not None:
            write_json(out_path, snap.to_dict(include_raw=include_raw))
            if args.do_print:
                print(f"\nSaved JSON: {out_path}")

    scope.close()

    if args.strict and missing:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

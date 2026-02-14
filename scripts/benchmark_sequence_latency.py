#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# Allow direct execution from repository root or scripts/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teledyne_lecroy import (
    AcquisitionConfig,
    ChannelConfig,
    ChannelTrigger,
    SequenceConfig,
    TriggerConfig,
    TriggerState,
    WR8208HD,
)


def run_once(
    scope: Any,
    *,
    channels_on: list[int],
    segments: int,
    tdiv: float,
    sampling_period: float,
    wait_timeout: float,
    opc_timeout: float,
    sync_mode: str,
    sn_mode: str,
    batch_segments: int,
    np_points: int,
    sp: int,
    display: str,
    postproc_profile: str,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "segments": segments,
        "points_per_segment": np_points,
        "channels_on": channels_on,
        "postproc_profile": postproc_profile,
        "display": display,
        "sync_mode": sync_mode,
        "sn_mode": sn_mode,
        "timeout_flag": 0,
        "bytes_received": 0,
    }

    t_start = time.perf_counter()
    try:
        scope.apply_settings({"instrument": {"display": display}})
        scope.configure(
            channels={ch: ChannelConfig(vdiv=0.1, offset=0.0, enabled=True) for ch in channels_on},
            acquisition=AcquisitionConfig(tdiv=tdiv, sampling_period=sampling_period),
            sequence=SequenceConfig(
                enabled=True,
                num_segments=segments,
                timeout_enabled=True,
                timeout_seconds=wait_timeout,
            ),
        )
        scope.set_trigger(
            TriggerConfig(
                channels={1: ChannelTrigger(state=TriggerState.HIGH, level=0.0)},
                mode="NORM",
                external=False,
            )
        )
        scope.set_trigger_mode("NORM")

        scope.arm(force=False)
        t_arm = time.perf_counter()
        scope.wait_for_trigger(timeout=wait_timeout, force=False)
        t_wait = time.perf_counter()

        t_opc = t_wait
        if sync_mode == "wait_then_opc":
            instr = getattr(scope, "_scope", None)
            prev_timeout = getattr(instr, "timeout", None)
            if instr is not None and prev_timeout is not None:
                instr.timeout = int(opc_timeout * 1000)
            try:
                scope.query("*OPC?")
                t_opc = time.perf_counter()
            finally:
                if instr is not None and prev_timeout is not None:
                    instr.timeout = prev_timeout

        instr = getattr(scope, "_scope", None)
        if instr is None:
            raise RuntimeError("scope backend missing _scope transport")
        ch0 = channels_on[0]

        t_firstbyte: float | None = None
        t_xfer_done: float | None = None
        total_bytes = 0
        if sn_mode == "all":
            scope.write(f"WFSU SP,{sp},NP,{np_points},FP,0,SN,0")
            instr.write(f"C{ch0}:WF? DAT1")
            raw = instr.read_raw()
            now = time.perf_counter()
            t_firstbyte = now
            t_xfer_done = now
            total_bytes += len(raw)
        elif sn_mode == "batch":
            points_per_batch = max(1, np_points * max(1, batch_segments))
            total_points = max(1, np_points * max(1, segments))
            point_offset = 0
            while point_offset < total_points:
                count = min(points_per_batch, total_points - point_offset)
                instr.write(f"C{ch0}:WF? DAT1,NO,{point_offset},NP,{count}")
                raw = instr.read_raw()
                now = time.perf_counter()
                if t_firstbyte is None:
                    t_firstbyte = now
                t_xfer_done = now
                total_bytes += len(raw)
                if not raw:
                    break
                point_offset += count
        else:
            for sn in range(1, segments + 1):
                scope.write(f"WFSU SP,{sp},NP,{np_points},FP,0,SN,{sn}")
                instr.write(f"C{ch0}:WF? DAT1")
                raw = instr.read_raw()
                now = time.perf_counter()
                if t_firstbyte is None:
                    t_firstbyte = now
                t_xfer_done = now
                total_bytes += len(raw)

        if t_firstbyte is None or t_xfer_done is None:
            raise RuntimeError("No waveform bytes received")

        rec["bytes_received"] = total_bytes
        rec["t_wait"] = t_wait - t_arm
        rec["t_opc"] = t_opc - t_arm
        rec["t_firstbyte"] = t_firstbyte - t_arm
        rec["t_xfer_done"] = t_xfer_done - t_arm
        rec["elapsed_total"] = time.perf_counter() - t_start
        return rec
    except Exception as exc:  # noqa: BLE001
        rec["timeout_flag"] = 1
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec.setdefault("t_wait", -1.0)
        rec.setdefault("t_opc", -1.0)
        rec.setdefault("t_firstbyte", -1.0)
        rec.setdefault("t_xfer_done", -1.0)
        rec["elapsed_total"] = time.perf_counter() - t_start
        return rec


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Benchmark sequence latency breakdown.")
    p.add_argument("--address", default="localhost")
    p.add_argument("--protocol", choices=["lxi", "vicp"], default="vicp")
    p.add_argument("--segments", type=int, default=10)
    p.add_argument("--np", type=int, default=1000)
    p.add_argument("--sp", type=int, default=1)
    p.add_argument("--channels", type=int, nargs="+", default=list(range(1, 9)))
    p.add_argument("--tdiv", type=float, default=1e-9)
    p.add_argument("--sampling-period", type=float, default=1e-10)
    p.add_argument("--wait-timeout", type=float, default=4.0)
    p.add_argument("--opc-timeout", type=float, default=4.0)
    p.add_argument("--sync-mode", choices=["wait_only", "wait_then_opc"], default="wait_then_opc")
    p.add_argument("--sn-mode", choices=["all", "loop", "batch"], default="all")
    p.add_argument("--batch-segments", type=int, default=100)
    p.add_argument("--display", choices=["ON", "OFF"], default="OFF")
    p.add_argument("--postproc-profile", choices=["minimal", "default"], default="minimal")
    p.add_argument("--out", type=Path, default=None, help="Append JSON line to this file")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    scope = WR8208HD(args.address, protocol=args.protocol, timeout=20.0, active_channels=args.channels)
    with scope:
        rec = run_once(
            scope,
            channels_on=args.channels,
            segments=args.segments,
            tdiv=args.tdiv,
            sampling_period=args.sampling_period,
            wait_timeout=args.wait_timeout,
            opc_timeout=args.opc_timeout,
            sync_mode=args.sync_mode,
            sn_mode=args.sn_mode,
            batch_segments=args.batch_segments,
            np_points=args.np,
            sp=args.sp,
            display=args.display,
            postproc_profile=args.postproc_profile,
        )

    line = json.dumps(rec, ensure_ascii=True)
    print(line)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import pyvisa
except ModuleNotFoundError:  # pragma: no cover - optional in some envs
    pyvisa = None

from teledyne_lecroy import (
    AcquisitionConfig,
    ChannelConfig,
    ChannelTrigger,
    SequenceConfig,
    TriggerConfig,
    TriggerState,
    WP804HD,
    WR8208HD,
)


def _probe_idn(address: str, protocol: str, timeout_s: float = 5.0) -> str:
    if pyvisa is None:
        raise RuntimeError("pyvisa is required for --model auto probing")
    rm = pyvisa.ResourceManager()
    resource = f"VICP::{address}::INSTR" if protocol == "vicp" else f"TCPIP0::{address}::inst0::INSTR"
    scope = None
    try:
        scope = rm.open_resource(resource)
        scope.timeout = int(timeout_s * 1000)
        return str(scope.query("*IDN?")).strip()
    finally:
        if scope is not None:
            scope.close()
        rm.close()


def _parse_int_list(value: str) -> list[int]:
    out: list[int] = []
    for raw in value.split(","):
        token = raw.strip().lower().replace("_", "")
        if not token:
            continue
        if token.endswith("ms"):
            out.append(int(float(token[:-2]) * 1_000_000))
        elif token.endswith("m"):
            out.append(int(float(token[:-1]) * 1_000_000))
        elif token.endswith("ks"):
            out.append(int(float(token[:-2]) * 1_000))
        elif token.endswith("k"):
            out.append(int(float(token[:-1]) * 1_000))
        elif token.endswith("s"):
            out.append(int(float(token[:-1])))
        else:
            out.append(int(float(token)))
    if not out:
        raise ValueError("empty list")
    return out


def _parse_display_list(value: str) -> list[str]:
    vals = [x.strip().upper() for x in value.split(",") if x.strip()]
    if not vals:
        raise ValueError("empty display list")
    for v in vals:
        if v not in {"ON", "OFF"}:
            raise ValueError(f"invalid display: {v}")
    return vals


def _run_case(
    *,
    scope_model: str,
    address: str,
    protocol: str,
    channels: list[int],
    segments: int,
    tdiv: float,
    sampling_period: float,
    max_samples: int,
    np_points: int,
    sp: int,
    display: str,
    with_copy: bool,
    wait_timeout: float,
) -> dict[str, Any]:
    scope_cls = WP804HD if scope_model == "wp804hd" else WR8208HD
    row: dict[str, Any] = {
        "case": "copy_then_rearm" if with_copy else "no_copy_rearm",
        "scope_model": scope_model,
        "segments": segments,
        "display": display,
        "channels_on": channels,
        "tdiv_s": tdiv,
        "sampling_period_req_s": sampling_period,
        "max_samples_req": max_samples,
        "timeout_flag": 0,
    }

    t0 = time.perf_counter()
    try:
        scope = scope_cls(
            address,
            protocol=protocol,
            timeout=max(60.0, wait_timeout + 30.0),
            active_channels=channels,
        )
        with scope:
            row["idn"] = scope.query("*IDN?").strip()
            scope.clear_sweeps()
            scope.apply_settings({"instrument": {"display": display}})
            scope.configure(
                channels={ch: ChannelConfig(vdiv=0.1, offset=0.0, enabled=True) for ch in channels},
                acquisition=AcquisitionConfig(
                    tdiv=tdiv,
                    sampling_period=sampling_period,
                    trigger_delay=0.0,
                    window_delay=0.0,
                    acquisition_mode="set_maximum_memory",
                ),
                sequence=SequenceConfig(
                    enabled=True,
                    num_segments=segments,
                    timeout_enabled=True,
                    timeout_seconds=wait_timeout,
                ),
            )
            # Explicitly apply memory depth cap as scope memory setting.
            scope.write(f"MSIZ {int(max_samples)}")
            try:
                scope.query("*OPC?")
            except Exception:
                pass
            scope.set_trigger(
                TriggerConfig(
                    channels={1: ChannelTrigger(state=TriggerState.HIGH, level=0.0)},
                    mode="SINGLE",
                    external=False,
                )
            )
            scope.set_trigger_mode("SINGLE")

            acq = scope.settings.get("acquisition", {})
            resolved_sampling = float(acq.get("sampling_period", sampling_period))
            resolved_tdiv = float(acq.get("tdiv", tdiv))
            resolved_points = int(round(scope.TIME_DIVISIONS * resolved_tdiv / resolved_sampling))
            row["sampling_period_resolved_s"] = resolved_sampling
            row["ps_per_point_resolved"] = resolved_sampling * 1e12
            row["max_samples_resolved"] = acq.get("max_samples")
            row["points_resolved"] = resolved_points
            row["msiz_readback"] = scope.query("MSIZ?").strip()
            row["trse"] = scope.query("TRSE?").strip()

            scope.arm(force=False)
            t_armed = time.perf_counter()
            scope.wait_for_trigger(timeout=wait_timeout, force=False)
            t_wait_done = time.perf_counter()

            t_copy_done = t_wait_done
            bytes_received = 0
            if with_copy:
                instr = getattr(scope, "_scope", None)
                if instr is None:
                    raise RuntimeError("scope backend missing _scope transport")
                scope.write(f"WFSU SP,{sp},NP,{np_points},FP,0,SN,0")
                for ch in channels:
                    instr.write(f"C{ch}:WF? DAT1")
                    raw = instr.read_raw()
                    bytes_received += len(raw)
                    t_copy_done = time.perf_counter()
                    if not raw:
                        break

            t_rearm0 = time.perf_counter()
            scope.set_trigger_mode("SINGLE")
            scope.arm(force=False)
            t_rearm1 = time.perf_counter()

            row.update(
                {
                    "t_wait_s": t_wait_done - t_armed,
                    "t_copy_window_s": t_copy_done - t_wait_done,
                    "t_rearm_cmd_s": t_rearm1 - t_rearm0,
                    "t_cycle_to_rearm_s": t_rearm1 - t_armed,
                    "bytes_received": bytes_received,
                    "elapsed_s": time.perf_counter() - t0,
                }
            )
    except Exception as exc:  # noqa: BLE001
        row["timeout_flag"] = 1
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["elapsed_s"] = time.perf_counter() - t0
    return row


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Benchmark sequence performance across max_samples settings.")
    p.add_argument("--address", default="localhost")
    p.add_argument("--protocol", choices=["lxi", "vicp"], default="vicp")
    p.add_argument("--model", choices=["auto", "wr8208hd", "wp804hd"], default="auto")
    p.add_argument("--channels", default="1,2,3,4,5,6,7,8")
    p.add_argument("--segments", type=int, default=10000)
    p.add_argument("--max-samples", default="500,1k,2k,5k,10k,25k,50k,100k,250k,500k,1m,2.5m")
    p.add_argument("--display", default="ON,OFF")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--tdiv", type=float, default=1e-9)
    p.add_argument("--sampling-period", type=float, default=100e-12)
    p.add_argument(
        "--sampling-mode",
        choices=["from-max", "fixed"],
        default="from-max",
        help="from-max: sampling_period = (10*tdiv)/max_samples, fixed: use --sampling-period 그대로",
    )
    p.add_argument("--np", type=int, default=100)
    p.add_argument("--sp", type=int, default=1)
    p.add_argument("--wait-timeout", type=float, default=120.0)
    p.add_argument(
        "--out",
        type=Path,
        default=Path("artifacts/sequence_benchmark/reports/sequence_max_samples.jsonl"),
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    scope_model = args.model
    if scope_model == "auto":
        idn = _probe_idn(args.address, args.protocol, timeout_s=5.0).upper()
        scope_model = "wp804hd" if "WP804" in idn else "wr8208hd"

    channels = [int(x.strip()) for x in args.channels.split(",") if x.strip()]
    if scope_model == "wp804hd" and args.channels.strip() == "1,2,3,4,5,6,7,8":
        channels = [1, 2, 3, 4]
    if scope_model == "wp804hd" and any(ch > 4 for ch in channels):
        raise ValueError("WP804HD supports channels 1..4 only. Use --channels 1,2,3,4")

    displays = _parse_display_list(args.display)
    max_samples_list = _parse_int_list(args.max_samples)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    for display in displays:
        for max_samples in max_samples_list:
            for with_copy in (False, True):
                for _ in range(args.repeat):
                    row = _run_case(
                        sampling_period=(
                            (10.0 * args.tdiv) / float(max_samples)
                            if args.sampling_mode == "from-max"
                            else args.sampling_period
                        ),
                        scope_model=scope_model,
                        address=args.address,
                        protocol=args.protocol,
                        channels=channels,
                        segments=args.segments,
                        tdiv=args.tdiv,
                        max_samples=max_samples,
                        np_points=args.np,
                        sp=args.sp,
                        display=display,
                        with_copy=with_copy,
                        wait_timeout=args.wait_timeout,
                    )
                    line = json.dumps(row, ensure_ascii=True)
                    print(line, flush=True)
                    with args.out.open("a", encoding="utf-8") as f:
                        f.write(line + "\n")
    print(f"DONE {args.out}", flush=True)


if __name__ == "__main__":
    main()

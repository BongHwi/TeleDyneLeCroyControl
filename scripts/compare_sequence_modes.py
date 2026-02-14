#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import sys
from pathlib import Path
from statistics import median
from typing import Any

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


def _parse_int_list(value: str) -> list[int]:
    return [int(x.strip()) for x in value.split(",") if x.strip()]


def _parse_mode_list(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _run_once(
    *,
    address: str,
    protocol: str,
    channels: list[int],
    segments: int,
    mode: str,
    batch_segments: int,
    tdiv: float,
    sampling_period: float,
    wait_timeout: float,
    display: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "mode": mode,
        "segments": segments,
        "channels_on": channels,
        "batch_segments": batch_segments,
        "display": display,
        "timeout_flag": 0,
    }
    t0 = time.perf_counter()
    scope = WR8208HD(address, protocol=protocol, timeout=max(30.0, wait_timeout + 20.0), active_channels=channels)
    try:
        with scope:
            scope.apply_settings({"instrument": {"display": display}})
            scope.configure(
                channels={ch: ChannelConfig(vdiv=0.1, offset=0.0, enabled=True) for ch in channels},
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
            scope.arm(force=False)
            scope.wait_for_trigger(timeout=wait_timeout, force=False)

            t_read0 = time.perf_counter()
            data = scope.readout_sequence(
                channels=channels,
                sn_mode=mode,  # type: ignore[arg-type]
                batch_segments=batch_segments,
            )
            t_read1 = time.perf_counter()

            prof = scope.get_last_sequence_profile() or {}
            record["readout_ms"] = (t_read1 - t_read0) * 1000.0
            record["elapsed_ms"] = (time.perf_counter() - t0) * 1000.0
            record["segments_rx_ch1"] = len(data.get(1, [])) if 1 in data else -1
            record["profile"] = prof
    except Exception as exc:  # noqa: BLE001
        record["timeout_flag"] = 1
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["elapsed_ms"] = (time.perf_counter() - t0) * 1000.0
    return record


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    by_mode: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if int(row.get("timeout_flag", 1)) != 0:
            continue
        by_mode.setdefault(str(row["mode"]), []).append(row)

    summary: dict[str, dict[str, float]] = {}
    for mode, items in by_mode.items():
        readouts = [float(r.get("readout_ms", 0.0)) for r in items]
        ch_metrics: list[dict[str, Any]] = []
        for r in items:
            prof = r.get("profile") or {}
            chm = prof.get("channel_metrics") or {}
            if isinstance(chm, dict):
                ch_metrics.extend([v for v in chm.values() if isinstance(v, dict)])
        def _med(key: str) -> float:
            vals = [float(m.get(key, 0.0)) for m in ch_metrics if key in m]
            return median(vals) if vals else 0.0
        summary[mode] = {
            "median_readout_ms": median(readouts),
            "median_metadata_ms": _med("metadata_ms"),
            "median_transfer_ms": _med("transfer_ms"),
            "median_split_ms": _med("split_ms"),
            "samples": float(len(items)),
        }
    return summary


def _write_report(path: Path, rows: list[dict[str, Any]], summary: dict[str, dict[str, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Sequence Mode Comparison Report",
        "",
        f"- total runs: {len(rows)}",
        f"- successful runs: {sum(1 for r in rows if int(r.get('timeout_flag', 1)) == 0)}",
        "",
        "## Summary (median)",
        "",
        "| mode | readout_ms | metadata_ms | transfer_ms | split_ms | samples |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for mode in sorted(summary):
        s = summary[mode]
        lines.append(
            f"| {mode} | {s['median_readout_ms']:.2f} | {s['median_metadata_ms']:.2f} | "
            f"{s['median_transfer_ms']:.2f} | {s['median_split_ms']:.2f} | {int(s['samples'])} |"
        )
    lines.append("")
    lines.append("## Raw Rows")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(rows, ensure_ascii=True, indent=2))
    lines.append("```")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compare WR8208HD sequence readout modes.")
    p.add_argument("--address", default="localhost")
    p.add_argument("--protocol", choices=["lxi", "vicp"], default="vicp")
    p.add_argument("--segments", default="2000,5000", help="Comma-separated list, e.g. 2000,5000")
    p.add_argument("--modes", default="all,batch", help="Comma-separated list: all,batch,loop,auto")
    p.add_argument("--batch-segments", type=int, default=100)
    p.add_argument("--channels", default="1,2,3,4,5,6,7,8")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--tdiv", type=float, default=1e-9)
    p.add_argument("--sampling-period", type=float, default=1e-10)
    p.add_argument("--wait-timeout", type=float, default=5.0)
    p.add_argument("--display", choices=["ON", "OFF"], default="OFF")
    p.add_argument("--out-jsonl", type=Path, default=Path("artifacts/sequence_benchmark/reports/mode_compare.jsonl"))
    p.add_argument("--out-report", type=Path, default=Path("artifacts/sequence_benchmark/reports/mode_compare.md"))
    return p.parse_args()


def main() -> None:
    args = parse_args()
    segments_list = _parse_int_list(args.segments)
    modes = _parse_mode_list(args.modes)
    channels = _parse_int_list(args.channels)

    rows: list[dict[str, Any]] = []
    args.out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    for segments in segments_list:
        for mode in modes:
            for _ in range(args.repeat):
                row = _run_once(
                    address=args.address,
                    protocol=args.protocol,
                    channels=channels,
                    segments=segments,
                    mode=mode,
                    batch_segments=args.batch_segments,
                    tdiv=args.tdiv,
                    sampling_period=args.sampling_period,
                    wait_timeout=args.wait_timeout,
                    display=args.display,
                )
                rows.append(row)
                line = json.dumps(row, ensure_ascii=True)
                print(line)
                with args.out_jsonl.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")

    summary = _aggregate(rows)
    _write_report(args.out_report, rows, summary)
    print(f"wrote report: {args.out_report}")


if __name__ == "__main__":
    main()

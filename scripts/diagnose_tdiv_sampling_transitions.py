#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teledyne_lecroy import (
    AcquisitionConfig,
    ChannelConfig,
    SequenceConfig,
    WP804HD,
    WR8208HD,
)


def _parse_tdiv_list(value: str) -> list[float]:
    out: list[float] = []
    for tok in value.split(","):
        t = tok.strip().lower().replace("_", "")
        if not t:
            continue
        if t.endswith("ns"):
            out.append(float(t[:-2]) * 1e-9)
        elif t.endswith("us"):
            out.append(float(t[:-2]) * 1e-6)
        elif t.endswith("ms"):
            out.append(float(t[:-2]) * 1e-3)
        elif t.endswith("s"):
            out.append(float(t[:-1]))
        else:
            out.append(float(t))
    if not out:
        raise ValueError("empty tdiv list")
    return out


def _parse_points_list(value: str) -> list[int]:
    out: list[int] = []
    for tok in value.split(","):
        t = tok.strip().lower().replace("_", "")
        if not t:
            continue
        if t.endswith("ks"):
            out.append(int(float(t[:-2]) * 1000))
        elif t.endswith("k"):
            out.append(int(float(t[:-1]) * 1000))
        elif t.endswith("ms"):
            out.append(int(float(t[:-2]) * 1_000_000))
        elif t.endswith("m"):
            out.append(int(float(t[:-1]) * 1_000_000))
        elif t.endswith("s"):
            out.append(int(float(t[:-1])))
        else:
            out.append(int(float(t)))
    if not out:
        raise ValueError("empty points list")
    return out


def _parse_targets(value: str) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for tok in value.split(","):
        t = tok.strip()
        if not t:
            continue
        if "@" not in t:
            raise ValueError(f"target must be model@address: {t}")
        model, addr = t.split("@", 1)
        model_l = model.strip().lower()
        if model_l not in {"wp804hd", "wr8208hd"}:
            raise ValueError(f"unsupported model in target: {model}")
        targets.append((model_l, addr.strip()))
    if not targets:
        raise ValueError("empty targets")
    return targets


def _to_float(text: str) -> float:
    return float(text.strip().split()[-1])


def _parse_wavedesc_points(text: str) -> int | None:
    m = re.search(r"WAVE_ARRAY_COUNT\s*:\s*(\d+)", text, flags=re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_wavedesc_dx(text: str) -> float | None:
    m = re.search(r"HORIZ_INTERVAL\s*:\s*([+-]?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?)", text, flags=re.IGNORECASE)
    return float(m.group(1)) if m else None


def _parse_wavedesc_sample_width(text: str) -> int:
    m = re.search(r"COMM_TYPE\s*:\s*([A-Z0-9_]+)", text, flags=re.IGNORECASE)
    if not m:
        return 1
    token = m.group(1).upper()
    return 2 if token in {"WORD", "2"} else 1


def _parse_ieee4882_payload_length(raw: bytes) -> int | None:
    if not raw:
        return None
    hash_idx = raw.find(b"#")
    if hash_idx < 0:
        return None
    if len(raw) < hash_idx + 2:
        return None
    n_digits = raw[hash_idx + 1] - 48
    if n_digits < 0 or n_digits > 9:
        return None
    if len(raw) < hash_idx + 2 + n_digits:
        return None
    try:
        size = int(raw[hash_idx + 2 : hash_idx + 2 + n_digits].decode("ascii"))
    except Exception:
        return None
    return size


def _run_target(
    *,
    model: str,
    address: str,
    protocol: str,
    channels: list[int],
    display: str,
    sequence_segments: int,
    tdiv_list: list[float],
    max_samples_list: list[int],
    sampling_period_hint: float,
    out: Path,
) -> None:
    scope_cls = WP804HD if model == "wp804hd" else WR8208HD
    scope = scope_cls(address, protocol=protocol, timeout=20.0, active_channels=channels)
    with scope:
        idn = scope.query("*IDN?").strip()
        # Configure static parts first so per-step acquisition writes are not overwritten.
        scope.apply_settings({"instrument": {"display": display}})
        scope.clear_sweeps()
        scope.configure(
            channels={
                ch: ChannelConfig(vdiv=0.1, offset=0.0, enabled=True)
                for ch in channels
            },
            sequence=SequenceConfig(
                enabled=True,
                num_segments=sequence_segments,
                timeout_enabled=True,
                timeout_seconds=20.0,
            ),
        )
        for max_samples in max_samples_list:
            for tdiv in tdiv_list:
                row: dict[str, Any] = {
                    "model": model,
                    "address": address,
                    "idn": idn,
                    "display": display,
                    "sequence_segments": sequence_segments,
                    "channels_on": channels,
                    "max_samples_req": max_samples,
                    "tdiv_req_s": tdiv,
                    "sampling_hint_s": sampling_period_hint,
                    "timeout_flag": 0,
                }
                t0 = time.perf_counter()
                try:
                    scope.configure(
                        acquisition=AcquisitionConfig(
                            tdiv=tdiv,
                            sampling_period=sampling_period_hint,
                            trigger_delay=0.0,
                            window_delay=0.0,
                            max_samples=max_samples,
                            acquisition_mode="set_maximum_memory",
                        ),
                    )
                    tdiv_rb = _to_float(scope.query("TDIV?"))
                    msiz_txt = scope.query("MSIZ?").strip()
                    msiz_val = _to_float(msiz_txt)
                    acq_settings = scope.settings.get("acquisition", {})
                    sampling_setting = float(acq_settings.get("sampling_period", sampling_period_hint))
                    try:
                        sr = float(scope.query(r"""vbs? 'return=app.Acquisition.Horizontal.SampleRate' """).strip())
                    except Exception:
                        sr = 0.0
                    points_est = int(round(scope.TIME_DIVISIONS * tdiv_rb * sr)) if sr > 0 else -1
                    psp_est = (1.0 / sr) * 1e12 if sr > 0 else -1.0
                    points_from_settings = int(round(scope.TIME_DIVISIONS * tdiv_rb / sampling_setting))
                    psp_from_settings = sampling_setting * 1e12
                    wavedesc_points: int | None = None
                    wavedesc_dx: float | None = None
                    sample_width_bytes = 1
                    points_measured_dat1: int | None = None
                    ps_per_point_from_dat1_tdiv: float | None = None
                    dat1_payload_bytes: int | None = None
                    try:
                        scope.write("C1:WFSU SP,1,NP,0,FP,0,SN,1")
                        wdesc = scope.query("C1:INSPECT? 'WAVEDESC'")
                        wavedesc_points = _parse_wavedesc_points(wdesc)
                        wavedesc_dx = _parse_wavedesc_dx(wdesc)
                        sample_width_bytes = _parse_wavedesc_sample_width(wdesc)
                        instr = getattr(scope, "_scope", None)
                        if instr is not None:
                            instr.write("C1:WF? DAT1")
                            raw = instr.read_raw()
                            dat1_payload_bytes = _parse_ieee4882_payload_length(raw)
                            if dat1_payload_bytes is not None and sample_width_bytes > 0:
                                points_measured_dat1 = dat1_payload_bytes // sample_width_bytes
                                if points_measured_dat1 > 0:
                                    ps_per_point_from_dat1_tdiv = (
                                        scope.TIME_DIVISIONS * tdiv_rb * 1e12
                                    ) / float(points_measured_dat1)
                    except Exception:
                        pass

                    row.update(
                        {
                            "tdiv_rb_s": tdiv_rb,
                            "msiz_readback": msiz_txt,
                            "msiz_readback_points": msiz_val,
                            "sample_rate_rb_sps": sr,
                            "points_estimated": points_est,
                            "ps_per_point_estimated": psp_est,
                            "sampling_period_setting_s": sampling_setting,
                            "points_from_sampling_setting": points_from_settings,
                            "ps_per_point_from_sampling_setting": psp_from_settings,
                            "points_measured_wavedesc": wavedesc_points,
                            "ps_per_point_measured_wavedesc": (wavedesc_dx * 1e12) if wavedesc_dx is not None else None,
                            "sample_width_bytes": sample_width_bytes,
                            "dat1_payload_bytes": dat1_payload_bytes,
                            "points_measured_dat1": points_measured_dat1,
                            "ps_per_point_measured_dat1_from_tdiv": ps_per_point_from_dat1_tdiv,
                            "elapsed_s": time.perf_counter() - t0,
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    row["timeout_flag"] = 1
                    row["error"] = f"{type(exc).__name__}: {exc}"
                    row["elapsed_s"] = time.perf_counter() - t0

                line = json.dumps(row, ensure_ascii=True)
                print(line, flush=True)
                with out.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Diagnose TDIV/ps-per-point transition behavior.")
    p.add_argument("--targets", required=True, help="Comma-separated model@address, e.g. wp804hd@localhost,wr8208hd@10.0.0.5")
    p.add_argument("--protocol", choices=["lxi", "vicp"], default="vicp")
    p.add_argument("--channels", default="", help="Override channels (e.g. 1,2,3,4). Default by model.")
    p.add_argument("--display", choices=["ON", "OFF"], default="ON")
    p.add_argument("--sequence-segments", type=int, default=10000)
    p.add_argument("--tdiv-list", default="1ns,2ns,5ns,10ns,20ns,50ns")
    p.add_argument("--max-samples-list", default="500,1k")
    p.add_argument("--sampling-hint", type=float, default=100e-12, help="Initial sampling period hint")
    p.add_argument(
        "--out",
        type=Path,
        default=Path("artifacts/sequence_benchmark/reports/tdiv_transition_diagnostics.jsonl"),
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    targets = _parse_targets(args.targets)
    tdiv_list = _parse_tdiv_list(args.tdiv_list)
    max_samples_list = _parse_points_list(args.max_samples_list)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    for model, addr in targets:
        if args.channels.strip():
            channels = [int(x.strip()) for x in args.channels.split(",") if x.strip()]
        else:
            channels = [1, 2, 3, 4] if model == "wp804hd" else [1, 2, 3, 4, 5, 6, 7, 8]
        _run_target(
            model=model,
            address=addr,
            protocol=args.protocol,
            channels=channels,
            display=args.display,
            sequence_segments=args.sequence_segments,
            tdiv_list=tdiv_list,
            max_samples_list=max_samples_list,
            sampling_period_hint=args.sampling_hint,
            out=args.out,
        )
    print(f"DONE {args.out}", flush=True)


if __name__ == "__main__":
    main()

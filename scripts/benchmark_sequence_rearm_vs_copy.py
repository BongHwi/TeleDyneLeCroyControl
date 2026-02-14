#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import sys
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
    WP804HD,
    SequenceConfig,
    TriggerConfig,
    TriggerState,
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


def _render_template(command: str, *, channel: int, index: int, segments: int, display: str) -> str:
    return command.format(
        channel=channel,
        index=index,
        segments=segments,
        display=display,
    )


def _run_device_save_profile(
    scope: Any,
    *,
    profile: str,
    channels: list[int],
    segments: int,
    display: str,
) -> int:
    if profile == "none":
        return 0
    if profile not in {
        "wr8208_all_displayed_binary_byte",
        "wp804_all_displayed_binary_byte",
    }:
        raise RuntimeError(f"unsupported device save profile: {profile}")

    # Configure save behavior to match UI intent:
    # Save To: File, Source: All Displayed, Save Format: Binary, Byte
    setup_commands = [
        "STST C1,HDD,AUTO,ON,FORMAT,BINARY",
        "VBS 'app.SaveRecall.Waveform.SaveSource = \"AllDisplayed\"'",
        "VBS 'app.SaveRecall.Waveform.WaveFormat = \"Binary\"'",
        "VBS 'app.SaveRecall.Waveform.BinarySubFormat = \"Byte\"'",
    ]
    for cmd in setup_commands:
        scope.write(cmd)

    # Firmware token differs by version; try aliases for "All Displayed".
    save_tokens = ["ALL_DISPLAYED", "ALLDISPLAYED", "ALLDISP"]
    last_exc: Exception | None = None
    for token in save_tokens:
        try:
            scope.write(f"STO {token},FILE")
            return len(setup_commands) + 1
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    if last_exc is not None:
        raise RuntimeError(f"failed STO all-displayed aliases: {last_exc}") from last_exc
    return len(setup_commands)


def _parse_segment_list(value: str) -> list[int]:
    result: list[int] = []
    for raw in value.split(","):
        token = raw.strip().lower().replace("_", "")
        if not token:
            continue
        if token.endswith("k"):
            result.append(int(float(token[:-1]) * 1000.0))
        else:
            result.append(int(float(token)))
    if not result:
        raise ValueError("segments list is empty")
    return result


def _parse_display_list(value: str) -> list[str]:
    values = [x.strip().upper() for x in value.split(",") if x.strip()]
    if not values:
        raise ValueError("display list is empty")
    for d in values:
        if d not in ("ON", "OFF"):
            raise ValueError(f"invalid display value: {d}")
    return values


def _run_case(
    *,
    address: str,
    protocol: str,
    channels: list[int],
    segments: int,
    tdiv: float,
    sampling_period: float,
    np_points: int,
    sp: int,
    display: str,
    with_copy: bool,
    wait_timeout: float,
    trigger_source: str,
    clear_before_case: bool,
    do_device_save: bool,
    device_save_cmd_template: str,
    device_save_wait_opc: bool,
    device_save_profile: str,
    scope_model: str,
) -> dict[str, Any]:
    effective_trigger = "C1" if trigger_source == "C0" else trigger_source
    if with_copy and do_device_save:
        case_name = "copy_and_device_save_then_rearm"
    elif with_copy:
        case_name = "copy_then_rearm"
    elif do_device_save:
        case_name = "device_save_then_rearm"
    else:
        case_name = "no_copy_rearm"
    row: dict[str, Any] = {
        "case": case_name,
        "trigger_source_requested": trigger_source,
        "trigger_source": effective_trigger,
        "channels_on": channels,
        "segments": segments,
        "tdiv_s": tdiv,
        "sampling_period_s": sampling_period,
        "points_per_segment": np_points,
        "display": display,
        "clear_before_case": clear_before_case,
        "device_save": do_device_save,
        "device_save_profile": device_save_profile,
        "timeout_flag": 0,
    }
    t0 = time.perf_counter()
    try:
        scope_cls = WP804HD if scope_model == "wp804hd" else WR8208HD
        scope = scope_cls(
            address,
            protocol=protocol,
            timeout=max(60.0, wait_timeout + 30.0),
            active_channels=channels,
        )
        with scope:
            idn = scope.query("*IDN?").strip()
            row["idn"] = idn
            if clear_before_case:
                # Ensure previous sweeps/segments do not affect this case.
                scope.clear_sweeps()
            scope.apply_settings({"instrument": {"display": display}})
            scope.configure(
                channels={ch: ChannelConfig(vdiv=0.1, offset=0.0, enabled=True) for ch in channels},
                acquisition=AcquisitionConfig(
                    tdiv=tdiv,
                    sampling_period=sampling_period,
                    trigger_delay=0.0,
                    window_delay=0.0,
                ),
                sequence=SequenceConfig(
                    enabled=True,
                    num_segments=segments,
                    timeout_enabled=True,
                    timeout_seconds=wait_timeout,
                ),
            )
            if effective_trigger == "EXT":
                scope.set_trigger(TriggerConfig(channels={}, mode="SINGLE", external=True))
            else:
                scope.set_trigger(
                    TriggerConfig(
                        channels={1: ChannelTrigger(state=TriggerState.HIGH, level=0.0)},
                        mode="SINGLE",
                        external=False,
                    )
                )
            scope.set_trigger_mode("SINGLE")
            trse = scope.query("TRSE?").strip()
            trmd = scope.query("TRMD?").strip()
            expect_token = "EX" if effective_trigger == "EXT" else "C1"
            if expect_token not in trse.upper():
                if effective_trigger != "EXT":
                    # Some firmware keeps previous EX source; force internal source once.
                    scope.write("TRSE EDGE,SR,C1")
                    trse_retry = scope.query("TRSE?").strip()
                    if expect_token not in trse_retry.upper():
                        raise RuntimeError(
                            "trigger source mismatch: "
                            f"requested={effective_trigger}, trse={trse}, trse_retry={trse_retry}"
                        )
                    trse = trse_retry
                else:
                    raise RuntimeError(
                        f"trigger source mismatch: requested={effective_trigger}, trse={trse}"
                    )

            scope.arm(force=False)
            t_armed = time.perf_counter()
            scope.wait_for_trigger(timeout=wait_timeout, force=False)
            t_wait_done = time.perf_counter()

            instr = getattr(scope, "_scope", None)
            if with_copy and instr is None:
                raise RuntimeError("scope backend missing _scope transport")

            t_first_copy: float | None = None
            t_copy_done: float = t_wait_done
            total_bytes = 0
            if with_copy:
                scope.write(f"WFSU SP,{sp},NP,{np_points},FP,0,SN,0")
                for ch in channels:
                    instr.write(f"C{ch}:WF? DAT1")
                    raw = instr.read_raw()
                    now = time.perf_counter()
                    if t_first_copy is None:
                        t_first_copy = now
                    t_copy_done = now
                    total_bytes += len(raw)
                    if not raw:
                        break

            t_device_save_start = time.perf_counter()
            t_device_save_done = t_device_save_start
            device_save_count = 0
            if do_device_save:
                if device_save_cmd_template.strip():
                    if "{channel}" in device_save_cmd_template:
                        for idx, ch in enumerate(channels, start=1):
                            cmd = _render_template(
                                device_save_cmd_template,
                                channel=ch,
                                index=idx,
                                segments=segments,
                                display=display,
                            )
                            scope.write(cmd)
                            device_save_count += 1
                    else:
                        cmd = _render_template(
                            device_save_cmd_template,
                            channel=channels[0],
                            index=1,
                            segments=segments,
                            display=display,
                        )
                        scope.write(cmd)
                        device_save_count = 1
                else:
                    profile = device_save_profile
                    if profile == "auto":
                        upper_idn = idn.upper()
                        if "WP804HD" in upper_idn:
                            profile = "wp804_all_displayed_binary_byte"
                        else:
                            profile = "wr8208_all_displayed_binary_byte"
                    device_save_count = _run_device_save_profile(
                        scope,
                        profile=profile,
                        channels=channels,
                        segments=segments,
                        display=display,
                    )
                if device_save_wait_opc:
                    scope.query("*OPC?")
                t_device_save_done = time.perf_counter()

            t_rearm_cmd_start = time.perf_counter()
            scope.set_trigger_mode("SINGLE")
            scope.arm(force=False)
            t_rearm_cmd_done = time.perf_counter()

            row.update(
                {
                    "wait_timeout_s": wait_timeout,
                    "t_wait_s": t_wait_done - t_armed,
                    "t_first_copy_s": (t_first_copy - t_armed) if t_first_copy is not None else -1.0,
                    "t_copy_done_s": t_copy_done - t_armed,
                    "t_copy_window_s": t_copy_done - t_wait_done,
                    "t_device_save_s": t_device_save_done - t_device_save_start,
                    "t_device_save_window_s": t_device_save_done - t_wait_done,
                    "device_save_count": device_save_count,
                    "t_rearm_cmd_s": t_rearm_cmd_done - t_rearm_cmd_start,
                    "t_cycle_to_rearm_s": t_rearm_cmd_done - t_armed,
                    "bytes_received": total_bytes,
                    "trse": trse,
                    "trmd": trmd,
                    "elapsed_s": time.perf_counter() - t0,
                }
            )
    except Exception as exc:  # noqa: BLE001
        row["timeout_flag"] = 1
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["elapsed_s"] = time.perf_counter() - t0
    return row


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compare sequence no-copy rearm vs copy+rearm.")
    p.add_argument("--address", default="localhost")
    p.add_argument("--protocol", choices=["lxi", "vicp"], default="vicp")
    p.add_argument("--channels", default="1,2,3,4,5,6,7,8")
    p.add_argument("--model", choices=["auto", "wr8208hd", "wp804hd"], default="auto")
    p.add_argument("--segments", default="100,500,1k,2.5k,5k,10k")
    p.add_argument("--display", default="ON,OFF", help="Comma-separated list: ON,OFF")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--tdiv", type=float, default=1e-9)
    p.add_argument("--sampling-period", type=float, default=1e-10)
    p.add_argument("--np", type=int, default=100)
    p.add_argument("--sp", type=int, default=1)
    p.add_argument("--wait-timeout-base", type=float, default=40.0)
    p.add_argument("--wait-timeout-per-segment", type=float, default=0.03)
    p.add_argument("--trigger-source", choices=["C0", "C1", "EXT"], default="C1")
    p.add_argument(
        "--no-clear-before-case",
        action="store_true",
        help="Skip explicit CLSW before each benchmark case",
    )
    p.add_argument(
        "--include-device-save",
        action="store_true",
        help="Add internal instrument-save case(s) to the benchmark matrix",
    )
    p.add_argument(
        "--include-copy-plus-device-save",
        action="store_true",
        help="When --include-device-save is set, also run copy+device-save combined case",
    )
    p.add_argument(
        "--device-save-cmd",
        default="",
        help=(
            "SCPI/VBS command template for instrument-side save. "
            "Supports {channel},{index},{segments},{display} placeholders. "
            "If {channel} exists, command runs per channel; otherwise once per case."
        ),
    )
    p.add_argument(
        "--device-save-profile",
        choices=[
            "auto",
            "none",
            "wr8208_all_displayed_binary_byte",
            "wp804_all_displayed_binary_byte",
        ],
        default="auto",
        help="Built-in local-save profile used when --device-save-cmd is empty",
    )
    p.add_argument(
        "--device-save-no-opc",
        action="store_true",
        help="Do not wait *OPC? after device-save command(s)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("artifacts/sequence_benchmark/reports/sequence_rearm_vs_copy.jsonl"),
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
    segments_list = _parse_segment_list(args.segments)
    displays = _parse_display_list(args.display)
    cases: list[tuple[bool, bool]] = [(False, False), (True, False)]
    if args.include_device_save:
        cases.append((False, True))
        if args.include_copy_plus_device_save:
            cases.append((True, True))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    for display in displays:
        for segments in segments_list:
            wait_timeout = max(args.wait_timeout_base, float(segments) * args.wait_timeout_per_segment)
            for with_copy, do_device_save in cases:
                for _ in range(args.repeat):
                    row = _run_case(
                        address=args.address,
                        protocol=args.protocol,
                        channels=channels,
                        segments=segments,
                        tdiv=args.tdiv,
                        sampling_period=args.sampling_period,
                        np_points=args.np,
                        sp=args.sp,
                        display=display,
                        with_copy=with_copy,
                        wait_timeout=wait_timeout,
                        trigger_source=args.trigger_source,
                        clear_before_case=not args.no_clear_before_case,
                        do_device_save=do_device_save,
                        device_save_cmd_template=args.device_save_cmd,
                        device_save_wait_opc=not args.device_save_no_opc,
                        device_save_profile=args.device_save_profile,
                        scope_model=scope_model,
                    )
                    line = json.dumps(row, ensure_ascii=True)
                    print(line, flush=True)
                    with args.out.open("a", encoding="utf-8") as f:
                        f.write(line + "\n")

    print(f"DONE {args.out}", flush=True)


if __name__ == "__main__":
    main()

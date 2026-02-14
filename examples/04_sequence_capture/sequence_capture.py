#!/usr/bin/env python3
"""Sequence capture example - captures multiple segments using current scope settings."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend (no X window needed)
import matplotlib.pyplot as plt

# Add parent directory to path for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from teledyne_lecroy import ScopeConnectionError, ScopeTimeoutError, SequenceData, WaveformData, WP804HD, WR8208HD


def make_scope(model: str, address: str, protocol: str):
    """Create a scope instance based on model name."""
    model_key = model.lower()
    if model_key in {"wp804hd", "wavepro"}:
        return WP804HD(address, protocol=protocol)
    if model_key in {"wr8208hd", "waverunner"}:
        return WR8208HD(address, protocol=protocol)
    raise ValueError(f"Unknown model: {model}")


def plot_waveform(waveform: WaveformData, output_path: str, title: str) -> None:
    """Plot and save waveform data as PNG."""
    t = waveform.to_time()
    v = waveform.to_voltage()
    if v.size == 0:
        print(f"CH{waveform.channel} seg{waveform.segment}: empty waveform, skip plot")
        return
    print(
        f"CH{waveform.channel} stats: min={v.min():.3f} V, "
        f"max={v.max():.3f} V, mean={v.mean():.3f} V, p2p={(v.max()-v.min()):.3f} V"
    )

    plt.figure(figsize=(8, 4))
    plt.plot(t, v, linewidth=1)
    plt.title(title)
    plt.xlabel("Time (s)")
    plt.ylabel("Voltage (V)")
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()


def timed(name: str):
    """Context manager to measure and print execution time."""
    class Timer:
        def __enter__(self):
            self.start = time.perf_counter()
            return self
        def __exit__(self, *args):
            elapsed = time.perf_counter() - self.start
            print(f"  [{name}] {elapsed*1000:.1f} ms")
    return Timer()


def save_sequence(seq: SequenceData, filepath: Path) -> None:
    """Save sequence data to numpy .npz file."""
    # Use the library method which handles inhomogeneous segment lengths (trimming)
    voltages = seq.to_voltage_array()

    # Calculate time axis for one segment (assuming uniform)
    if len(seq) > 0:
        time_axis = seq[0].to_time()
        # Ensure time axis matches voltage array width (in case of trimming)
        if len(time_axis) > voltages.shape[1]:
            time_axis = time_axis[:voltages.shape[1]]
    else:
        time_axis = np.array([])

    np.savez_compressed(
        filepath,
        voltages=voltages,  # Shape: (n_segments, n_points)
        time=time_axis,
        channel=seq.channel,
    )


def parse_args():
    p = argparse.ArgumentParser(description="Capture sequence waveforms using current scope settings.")
    p.add_argument(
        "--model",
        choices=["wp804hd", "wr8208hd", "wavepro", "waverunner"],
        default="wp804hd",
    )
    p.add_argument("--address", default="192.168.0.10")
    p.add_argument("--protocol", choices=["lxi", "vicp"], default="lxi")
    p.add_argument("--outdir", type=Path, default=Path("."))
    p.add_argument("--segments", type=int, default=100, help="Number of segments to capture (default: 100)")
    p.add_argument("--channels", type=int, nargs="+", help="Channels to capture (default: all enabled)")
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to JSON settings file to apply (omit to read from scope and save to settings.json)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Use force trigger for testing without real signals",
    )
    p.add_argument(
        "--wait-timeout",
        type=float,
        default=None,
        help="Wait timeout in seconds (default: use settings.sequence.timeout_seconds if available)",
    )
    p.add_argument(
        "--sync",
        choices=["wait", "wait-opc"],
        default="wait",
        help="Synchronization strategy: wait only, or wait then *OPC? for processing completion.",
    )
    p.add_argument(
        "--opc-timeout",
        type=float,
        default=3.0,
        help="Timeout in seconds for *OPC? when --sync wait-opc.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)

    try:
        with make_scope(args.model, args.address, args.protocol) as scope:
            # Handle settings: apply from file or save current
            if args.config:
                print(f"Loading settings from: {args.config}")
                with args.config.open() as f:
                    settings = json.load(f)
                scope.apply_settings(settings)
                print("Settings applied.")
            else:
                settings = scope.read_all_settings()
                output_path = Path("settings.json")
                with output_path.open("w") as f:
                    json.dump(settings, f, indent=2)
                print(f"Current settings saved to: {output_path}")

            # Enable sequence mode with specified segment count
            scope.apply_settings({
                "sequence": {
                    "enabled": True,
                    "num_segments": args.segments,
                }
            })

            # Determine channels to capture
            if args.channels:
                channels = args.channels
            else:
                settings = scope.read_all_settings()
                channels = [
                    int(ch) for ch, cfg in settings["channels"].items()
                    if cfg.get("enabled")
                ]
            if not channels:
                channels = [1]

            print(f"Capturing channels: {channels}")
            print(f"Arming for sequence capture ({args.segments} segments)...")
            wait_timeout = args.wait_timeout
            if wait_timeout is None:
                wait_timeout = float(settings.get("sequence", {}).get("timeout_seconds", 10.0))
            print(f"Wait timeout: {wait_timeout:.1f}s")

            # Force NORM mode - re-arms after each trigger (needed for multi-segment capture)
            scope.set_trigger_mode("NORM")

            with timed("arm"):
                scope.arm()

            with timed("wait_for_trigger"):
                scope.wait_for_trigger(timeout=wait_timeout, force=args.force)
            print("Triggered.")

            sync_mode = getattr(args, "sync", "wait")
            opc_timeout = float(getattr(args, "opc_timeout", 3.0))
            timeout_flag = 0
            if sync_mode == "wait-opc":
                print(f"Sync mode: wait-opc (opc timeout={opc_timeout:.1f}s)")
                try:
                    with timed("opc"):
                        scope.wait_opc(timeout=opc_timeout)
                except Exception as exc:
                    timeout_flag = 1
                    print(f"*OPC? timeout/error: {exc}")
            else:
                print("Sync mode: wait")

            with timed("readout_sequence"):
                data = scope.readout_sequence(channels=channels)

            # Data size diagnostics
            print("\n=== Data Size ===")
            total_bytes = 0
            for ch, seq in data.items():
                ch_bytes = sum(len(seg.raw_data) for seg in seq.segments)
                total_bytes += ch_bytes
                points_per_seg = len(seq[0].raw_data) if len(seq) > 0 else 0
                print(f"  CH{ch}: {len(seq)} segments × {points_per_seg:,} points = {ch_bytes:,} bytes ({ch_bytes/1e6:.2f} MB)")
            print(f"  Total: {total_bytes:,} bytes ({total_bytes/1e6:.2f} MB)")
            print(f"  timeout_flag: {timeout_flag}")
            print()

            for ch, seq in data.items():
                print(f"CH{ch}: {len(seq)} segments")

                # Save all sequence data
                data_file = args.outdir / f"sequence_ch{ch}.npz"
                with timed(f"save_data_ch{ch}"):
                    save_sequence(seq, data_file)
                print(f"Saved data: {data_file.name}")

                # Plot first 3 segments as a sanity check
                for idx in range(min(3, len(seq))):
                    wf = seq[idx]
                    with timed(f"plot_ch{ch}_seg{idx}"):
                        plot_waveform(
                            wf,
                            str(args.outdir / f"sequence_ch{ch}_seg{idx}.png"),
                            title=f"Sequence CH{ch} Seg {idx}",
                        )
                    print(f"Saved plot: sequence_ch{ch}_seg{idx}.png")

    except (ScopeConnectionError, ScopeTimeoutError) as e:
        print(f"Sequence capture failed: {e}")
        raise


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Single capture example - captures one waveform using current scope settings."""
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

from teledyne_lecroy import ScopeConnectionError, ScopeTimeoutError, WaveformData, WavePro, WaveRunner


def make_scope(model: str, address: str, protocol: str):
    """Create a scope instance based on model name."""
    if model == "wavepro":
        return WavePro(address, protocol=protocol)
    if model == "waverunner":
        return WaveRunner(address, protocol=protocol)
    raise ValueError(f"Unknown model: {model}")


def plot_waveform(waveform: WaveformData, output_path: str, title: str) -> None:
    """Plot and save waveform data as PNG."""
    t = waveform.to_time()
    v = waveform.to_voltage()
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


def save_waveform(wf: WaveformData, filepath: Path) -> None:
    """Save waveform data to numpy .npz file."""
    np.savez_compressed(
        filepath,
        time=wf.to_time(),
        voltage=wf.to_voltage(),
        raw=np.frombuffer(wf.raw_data, dtype=np.int8),
        dx=wf.dx,
        x0=wf.x0,
        dy=wf.dy,
        y0=wf.y0,
        channel=wf.channel,
        segment=wf.segment,
    )


def parse_args():
    p = argparse.ArgumentParser(description="Capture single waveform using current scope settings.")
    p.add_argument("--model", choices=["wavepro", "waverunner"], default="wavepro")
    p.add_argument("--address", default="192.168.0.10")
    p.add_argument("--protocol", choices=["lxi", "vicp"], default="lxi")
    p.add_argument("--outdir", type=Path, default=Path("."))
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

            # Disable sequence mode for single capture
            scope.apply_settings({"sequence": {"enabled": False}})

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
            print("Arming for single capture...")

            # Force SINGLE mode - triggers once then stops (TRMD? returns "STOP")
            # This ensures is_triggered() works regardless of settings.json trigger.mode
            scope.set_trigger_mode("SINGLE")

            with timed("arm"):
                scope.arm()

            with timed("wait_for_trigger"):
                scope.wait_for_trigger(timeout=10.0, force=args.force)
            print("Triggered.")

            with timed("readout"):
                data = scope.readout(channels=channels)

            # Data size diagnostics
            print("\n=== Data Size ===")
            total_bytes = 0
            for ch, wf in data.items():
                raw_bytes = len(wf.raw_data)
                total_bytes += raw_bytes
                print(f"  CH{ch}: {raw_bytes:,} bytes ({raw_bytes/1e6:.2f} MB), {raw_bytes:,} points")
            print(f"  Total: {total_bytes:,} bytes ({total_bytes/1e6:.2f} MB)")
            print()

            for ch, wf in data.items():
                # Save data
                data_file = args.outdir / f"single_capture_ch{ch}.npz"
                with timed(f"save_data_ch{ch}"):
                    save_waveform(wf, data_file)
                print(f"Saved data: {data_file.name}")

                # Save plot
                with timed(f"plot_ch{ch}"):
                    plot_waveform(
                        wf,
                        str(args.outdir / f"single_capture_ch{ch}.png"),
                        title=f"Single Capture CH{ch}",
                    )
                print(f"Saved plot: single_capture_ch{ch}.png")

    except (ScopeConnectionError, ScopeTimeoutError) as e:
        print(f"Capture failed: {e}")
        raise


if __name__ == "__main__":
    main()

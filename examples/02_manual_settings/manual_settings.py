#!/usr/bin/env python3
"""Manual settings management for Teledyne LeCroy oscilloscopes.

Two modes of operation:
- Read mode (no --config): Read current scope settings and save to JSON file
- Apply mode (with --config): Apply settings from JSON file to scope

Supports partial config files - only sections present in the file are applied.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add parent directory to path for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from teledyne_lecroy import (
    ScopeConfigurationError,
    ScopeConnectionError,
    WP804HD,
    WR8208HD,
)


def make_scope(model: str, address: str, protocol: str):
    """Create a scope instance based on model name."""
    model_key = model.lower()
    if model_key in {"wp804hd", "wavepro"}:
        return WP804HD(address, protocol=protocol)
    if model_key in {"wr8208hd", "waverunner"}:
        return WR8208HD(address, protocol=protocol)
    raise ValueError(f"Unknown model: {model}")


def parse_args():
    p = argparse.ArgumentParser(
        description="Read or apply oscilloscope settings.",
        epilog="Without --config, reads current settings from scope and saves to file.",
    )
    p.add_argument(
        "--model",
        choices=["wp804hd", "wr8208hd", "wavepro", "waverunner"],
        default="wp804hd",
    )
    p.add_argument("--address", default="192.168.0.10")
    p.add_argument("--protocol", choices=["lxi", "vicp"], default="lxi")
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to JSON settings file to apply (omit to read from scope)",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "settings.json",
        help="Output path for read mode (default: settings.json)",
    )
    p.add_argument(
        "-f", "--force",
        action="store_true",
        help="Overwrite output file without confirmation",
    )
    return p.parse_args()


def read_settings(scope, output_path: Path, force: bool) -> None:
    """Read current scope settings and save to JSON file."""
    if output_path.exists() and not force:
        response = input(f"'{output_path}' exists. Overwrite? [y/N]: ")
        if response.lower() not in ("y", "yes"):
            print("Aborted.")
            return

    settings = scope.read_all_settings()
    with output_path.open("w") as f:
        json.dump(settings, f, indent=2)
    print(f"Settings saved to: {output_path}")


def apply_settings(scope, config_path: Path) -> None:
    """Apply settings from JSON file to scope."""
    print(f"Loading settings from: {config_path}")
    with config_path.open() as f:
        settings = json.load(f)

    # Show which sections will be applied
    sections = [k for k in settings.keys() if k in (
        "instrument", "channels", "acquisition", "trigger", "sequence", "auxiliary_output"
    )]
    print(f"Applying sections: {', '.join(sections)}")

    scope.apply_settings(settings)
    print("Settings applied. Verify changes on the scope front panel.")


def main() -> None:
    args = parse_args()
    try:
        with make_scope(args.model, args.address, args.protocol) as scope:
            if args.config is None:
                # Read mode: save current scope settings to file
                read_settings(scope, args.output, args.force)
            else:
                # Apply mode: apply settings from file to scope
                apply_settings(scope, args.config)

    except (ScopeConnectionError, ScopeConfigurationError) as e:
        print(f"Error: {e}")
        raise


if __name__ == "__main__":
    main()

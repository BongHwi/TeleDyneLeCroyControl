#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Allow direct execution from repository root or scripts/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teledyne_lecroy import WP804HD, WR8208HD


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Check remote measurement/math visibility and optionally disable them."
    )
    p.add_argument("--model", choices=["wavepro", "waverunner"], default="wavepro")
    p.add_argument("--address", required=True)
    p.add_argument("--protocol", choices=["lxi", "vicp"], default="lxi")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--measurement-slots", type=int, default=12)
    p.add_argument("--math-traces", type=int, default=8)
    p.add_argument("--disable", action="store_true", help="Disable all measurement/math before readback")
    p.add_argument("--repeat", type=int, default=1, help="Number of check rounds")
    p.add_argument("--interval", type=float, default=0.5, help="Sleep seconds between rounds")
    p.add_argument("--retries", type=int, default=3, help="Retry count per round on connection/query failure")
    p.add_argument("--retry-interval", type=float, default=0.5, help="Sleep seconds between retries")
    return p.parse_args()


def make_scope(model: str, address: str, protocol: str, timeout: float):
    if model == "wavepro":
        return WP804HD(address, protocol=protocol, timeout=timeout)
    return WR8208HD(address, protocol=protocol, timeout=timeout)


def summarize(vis: dict[str, dict[int, bool]]) -> dict[str, object]:
    meas = vis.get("measurement", {})
    math = vis.get("math", {})
    meas_on = [idx for idx, state in meas.items() if state]
    math_on = [idx for idx, state in math.items() if state]
    return {
        "measurement_on": meas_on,
        "math_on": math_on,
        "measurement_all_off": len(meas_on) == 0,
        "math_all_off": len(math_on) == 0,
    }


def main() -> None:
    args = parse_args()
    for i in range(args.repeat):
        last_error: str | None = None
        success = False
        for attempt in range(args.retries + 1):
            try:
                scope = make_scope(args.model, args.address, args.protocol, args.timeout)
                with scope:
                    if args.disable:
                        scope.disable_measurement_and_math()

                    vis = scope.read_measurement_math_visibility(
                        measurement_slots=args.measurement_slots,
                        math_traces=args.math_traces,
                    )
                out = {
                    "round": i + 1,
                    "attempt": attempt + 1,
                    "timestamp": time.time(),
                    **summarize(vis),
                    "visibility": vis,
                }
                print(json.dumps(out, ensure_ascii=True))
                success = True
                break
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < args.retries and args.retry_interval > 0:
                    time.sleep(args.retry_interval)

        if not success:
            err = {
                "round": i + 1,
                "attempt": args.retries + 1,
                "timestamp": time.time(),
                "error": last_error or "unknown_error",
            }
            print(json.dumps(err, ensure_ascii=True))

        if i + 1 < args.repeat and args.interval > 0:
            time.sleep(args.interval)


if __name__ == "__main__":
    main()

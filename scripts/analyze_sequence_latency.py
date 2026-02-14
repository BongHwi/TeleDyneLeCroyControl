#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median
from typing import Any


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def derive(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["postproc_s"] = float(out.get("t_opc", -1.0)) - float(out.get("t_wait", -1.0))
    out["packaging_s"] = float(out.get("t_firstbyte", -1.0)) - float(out.get("t_opc", -1.0))
    out["transfer_s"] = float(out.get("t_xfer_done", -1.0)) - float(out.get("t_firstbyte", -1.0))
    return out


def suggest_fine(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [derive(r) for r in rows if int(r.get("timeout_flag", 1)) == 0]
    by_seg: dict[int, list[float]] = {}
    for r in ok:
        seg = int(r.get("segments", 0))
        by_seg.setdefault(seg, []).append(float(r.get("postproc_s", 0.0)))
    if not by_seg:
        return {"segments": [180, 190, 200, 210, 220, 230, 240, 250, 260], "np": [1000, 10000]}
    segs = sorted(by_seg)
    med = {s: median(by_seg[s]) for s in segs}
    knee = segs[len(segs) // 2]
    for i in range(1, len(segs)):
        prev, cur = segs[i - 1], segs[i]
        if med[cur] > med[prev] * 1.5:
            knee = cur
            break
    start = max(10, knee - 40)
    end = knee + 40
    around = list(range(start, end + 1, 10))
    np_vals = sorted({int(r.get("points_per_segment", 1000)) for r in ok})
    if not np_vals:
        np_vals = [1000, 10000]
    return {"segments": around, "np": np_vals[:2]}


def write_report(rows: list[dict[str, Any]], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    drows = [derive(r) for r in rows]
    ok = [r for r in drows if int(r.get("timeout_flag", 1)) == 0]
    if not ok:
        out.write_text("# Knee Summary\n\nNo successful rows found.\n", encoding="utf-8")
        return
    best = min(ok, key=lambda r: (r["postproc_s"] + r["packaging_s"] + r["transfer_s"]))
    lines = [
        "# Knee Summary",
        "",
        f"- rows: {len(rows)}",
        f"- success rows: {len(ok)}",
        f"- recommended sync_mode: `{best.get('sync_mode')}`",
        f"- recommended segments: `{best.get('segments')}`",
        f"- recommended NP: `{best.get('points_per_segment')}`",
        f"- recommended SN strategy: `{'loop' if best.get('packaging_s', 0.0) > best.get('postproc_s', 0.0) else 'all'}`",
        "",
        "## Best Row",
        "",
        "```json",
        json.dumps(best, ensure_ascii=True, indent=2),
        "```",
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_sn_report(rows: list[dict[str, Any]], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    drows = [derive(r) for r in rows if int(r.get("timeout_flag", 1)) == 0]
    all_rows = [r for r in drows if r.get("sn_mode") == "all"]
    loop_rows = [r for r in drows if r.get("sn_mode") == "loop"]
    lines = ["# SN Strategy Comparison", ""]
    if not all_rows or not loop_rows:
        lines.append("Insufficient data: need both `sn_mode=all` and `sn_mode=loop` successful rows.")
    else:
        all_pack = median([r["packaging_s"] for r in all_rows])
        loop_pack = median([r["packaging_s"] for r in loop_rows])
        lines.append(f"- median packaging(all): {all_pack:.6f}s")
        lines.append(f"- median packaging(loop): {loop_pack:.6f}s")
        lines.append("")
        if loop_pack < all_pack:
            lines.append("Recommendation: use `sn_mode=loop` (packaging-dominant latency).")
        else:
            lines.append("Recommendation: keep `sn_mode=all` (packaging overhead not dominant).")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Analyze sequence latency JSONL logs.")
    p.add_argument("--infile", type=Path, default=Path("artifacts/sequence_benchmark/coarse/coarse.jsonl"))
    p.add_argument("--suggest-fine", action="store_true")
    p.add_argument("--suggest-out", type=Path, default=Path("artifacts/sequence_benchmark/reports/fine_suggest.json"))
    p.add_argument("--report", type=Path, default=None)
    p.add_argument("--sn-report", type=Path, default=None)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    rows = load_jsonl(args.infile)

    if args.suggest_fine:
        suggestion = suggest_fine(rows)
        args.suggest_out.parent.mkdir(parents=True, exist_ok=True)
        args.suggest_out.write_text(json.dumps(suggestion, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(suggestion, ensure_ascii=True))

    if args.report is not None:
        write_report(rows, args.report)
        print(f"wrote report: {args.report}")

    if args.sn_report is not None:
        write_sn_report(rows, args.sn_report)
        print(f"wrote sn report: {args.sn_report}")


if __name__ == "__main__":
    main()

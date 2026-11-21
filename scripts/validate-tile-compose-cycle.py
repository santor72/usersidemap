#!/usr/bin/env python3
"""Measure separate manual CLIs against one explicit snapshot (never run sync)."""

import argparse
import json
import math
import subprocess
import time
from pathlib import Path
from uuid import UUID


def run(format, command, *args):
    started = time.monotonic()
    result = subprocess.run(
        [
            "docker",
            "compose",
            "run",
            "--rm",
            "--no-deps",
            f"tiles-{format}",
            command,
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        # Do not print unreviewed Docker configuration or connection errors.
        raise RuntimeError(
            f"{format}/{command} exit={result.returncode}; inspect worker state"
        )
    rows = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    if not rows:
        raise RuntimeError(f"{format}/{command}: missing CLI JSON report")
    return rows[-1], round(time.monotonic() - started, 3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=UUID, required=True)
    parser.add_argument(
        "--report", type=Path, default=Path("reports/tiles/deployment-cycle.json")
    )
    args = parser.parse_args()
    started = time.monotonic()
    formats = {}
    for format in ("vector", "raster"):
        print(f"{format}: full native bake", flush=True)
        bake, bake_seconds = run(format, format, "--dataset", str(args.dataset))
        version = str(UUID(bake["tileVersion"]))
        publication, publish_seconds = run(
            format, "publish", "--format", format, "--version", version
        )
        formats[format] = {
            "bakeSeconds": bake_seconds,
            "publishSeconds": publish_seconds,
            "bake": bake,
            "publication": publication,
        }
        print(
            f"{format}: published in {bake_seconds + publish_seconds:.3f}s", flush=True
        )
    total = round(time.monotonic() - started, 3)
    report = {
        "datasetVersion": str(args.dataset),
        "formats": formats,
        "totalSeconds": total,
        "recommendedLagGraceSeconds": max(210, math.ceil(total * 1.5 / 30) * 30),
        "limits": {"cpus": 2, "memoryMiB": 1024, "concurrency": 2},
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        f"Cycle {total}s; grace >= {report['recommendedLagGraceSeconds']}s; {args.report}"
    )


if __name__ == "__main__":
    main()

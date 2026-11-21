"""Measure preload and batch download through the public HTTP contract."""

import argparse
import gzip
import json
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def percentile(values, percentile_value):
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile_value / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def request_json(base_url, path, body=None):
    encoded = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    request = Request(
        base_url.rstrip("/") + path,
        data=encoded,
        method="GET" if encoded is None else "POST",
        headers={"Content-Type": "application/json"} if encoded else {},
    )
    try:
        with urlopen(request, timeout=120) as response:
            payload = response.read()
            return json.loads(payload), len(payload), payload
    except HTTPError as exc:
        raise RuntimeError(f"API returned HTTP {exc.code} for {path}") from None


def load_once(base_url, preload_body):
    started = time.perf_counter()
    manifest, _, _ = request_json(base_url, "/api/map/objects/preload", preload_body)
    preload_ms = (time.perf_counter() - started) * 1000
    payloads = []
    batch_times = []
    batch_bytes = []
    for batch in manifest["batches"]:
        tick = time.perf_counter()
        _, response_size, response_payload = request_json(
            base_url,
            "/api/map/objects/batch",
            {"queryId": manifest["queryId"], "batchIndex": batch["index"]},
        )
        batch_times.append((time.perf_counter() - tick) * 1000)
        batch_bytes.append(response_size)
        payloads.append(response_payload)
    return {
        "preload_ms": preload_ms,
        "first_batch_ms": batch_times[0] if batch_times else 0,
        "batch_download_ms": sum(batch_times),
        "full_load_ms": preload_ms + sum(batch_times),
        "objects": manifest["totalObjects"],
        "vertices": manifest["totalVertices"],
        "batch_count": len(manifest["batches"]),
        "response_bytes": sum(batch_bytes),
        "gzip_bytes": sum(
            len(gzip.compress(payload, compresslevel=6)) for payload in payloads
        ),
    }


def describe(values):
    return {
        "p50": round(statistics.median(values), 2),
        "p95": round(percentile(values, 95), 2),
    }


class MemorySampler:
    def __init__(self, pids):
        self.pids = [pid for pid in pids.split(",") if pid]
        self.peak_rss_kb = 0
        self.finished = threading.Event()
        self.thread = None

    def __enter__(self):
        if self.pids:
            self.thread = threading.Thread(target=self._sample, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, *_):
        self.finished.set()
        if self.thread:
            self.thread.join()

    def _sample(self):
        while not self.finished.is_set():
            for pid in self.pids:
                try:
                    result = subprocess.run(
                        ["ps", "-o", "rss=", "-p", pid],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    self.peak_rss_kb = max(
                        self.peak_rss_kb, int(result.stdout.strip() or 0)
                    )
                except (OSError, ValueError, subprocess.CalledProcessError):
                    continue
            self.finished.wait(0.1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--map-id", type=int, default=3)
    parser.add_argument("--bbox", default="[-180,-90,180,90]")
    parser.add_argument("--zoom", type=int, default=24)
    parser.add_argument("--types", default="cable_line,node,coupler")
    parser.add_argument("--layers", default="")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--server-pids", default="")
    args = parser.parse_args()

    layers = [int(value) for value in args.layers.split(",") if value]
    body = {
        "mapId": args.map_id,
        "bbox": json.loads(args.bbox),
        "zoom": args.zoom,
        "objectTypes": [value for value in args.types.split(",") if value],
        "layerIds": layers,
    }
    ready, _, _ = request_json(args.base_url, "/health/ready")
    with MemorySampler(args.server_pids) as memory:
        samples = [load_once(args.base_url, body) for _ in range(args.runs)]
        concurrent_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            concurrent_samples = list(
                executor.map(
                    lambda _: load_once(args.base_url, body), range(args.concurrency)
                )
            )
    keys = (
        "preload_ms",
        "first_batch_ms",
        "batch_download_ms",
        "full_load_ms",
        "response_bytes",
        "gzip_bytes",
    )
    report = {
        "postgis": ready.get("postgis"),
        "request": body,
        "objects": samples[-1]["objects"],
        "vertices": samples[-1]["vertices"],
        "batch_count": samples[-1]["batch_count"],
        "runs": len(samples),
        "timings_and_sizes": {
            key: describe([sample[key] for sample in samples]) for key in keys
        },
        "server_peak_rss_kb": memory.peak_rss_kb or None,
    }
    report["parallel_clients"] = {
        "clients": args.concurrency,
        "elapsed_ms": round((time.perf_counter() - concurrent_started) * 1000, 2),
        "full_load_ms": describe(
            [sample["full_load_ms"] for sample in concurrent_samples]
        ),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

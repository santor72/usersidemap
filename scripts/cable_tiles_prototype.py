#!/usr/bin/env python3
"""Disposable prototype only: import snapshot, bake measurements, browser serve.

Requires TILE_PROTOTYPE_DB_URL pointing to a disposable database. Never publishes
tile releases or modifies the original Userside/working map database.
"""

import argparse
import concurrent.futures
import gzip
import json
import os
import resource
import sys
import time
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from map_render.cables import render_attributes, visible_cable
from map_sync.migrate import migrate
from map_tiles.defaults import (
    CONCURRENCY,
    DISPLAY_MAXZOOM,
    RASTER_PREBAKE_MAX,
    TASKS_PER_PROCESS,
    VECTOR_MAXZOOM,
)
from map_tiles.geometry import canon, xyz
from map_tiles.planning import plan_zoom, selection_zoom
from map_tiles.raster import render_webp
from map_tiles.vector import EMPTY_MVT, encode_mvt, tile_inputs

ROOT = Path(__file__).resolve().parents[1]
_worker_conn = None


def connect():
    return psycopg.connect(
        os.environ["TILE_PROTOTYPE_DB_URL"], autocommit=True, row_factory=dict_row
    )


def init_worker():
    global _worker_conn
    _worker_conn = connect()


def import_snapshot(path):
    data = json.loads(path.read_text())
    dataset, run = uuid4(), uuid4()
    with connect() as conn:
        migrate(conn)
        with conn.transaction():
            conn.execute(
                """INSERT INTO map_data.sync_runs
                (id,source_system,map_id,status,rules_version)
                VALUES (%s,'tiles_prototype',3,'succeeded','prototype-v1')""",
                (run,),
            )
            conn.execute(
                """INSERT INTO map_data.datasets
                (id,run_id,source_system,map_id,status,config)
                VALUES (%s,%s,'tiles_prototype',3,'building',%s)""",
                (
                    dataset,
                    run,
                    Jsonb({"originalDatasetVersion": data.get("datasetVersion")}),
                ),
            )
            layers = sorted(
                {layer for f in data["features"] for layer in f.get("layer_ids", [])}
            )
            for layer in layers:
                conn.execute(
                    """INSERT INTO map_data.layers (dataset_id,source_id,title,default_visible)
                    VALUES (%s,%s,'prototype',true)""",
                    (dataset, layer),
                )
            for feature in data["features"]:
                conn.execute(
                    """INSERT INTO map_data.features
                    (dataset_id,source_kind,source_id,object_type,geom,min_zoom,hidden,
                     vertex_count,style,properties)
                    VALUES (%s,%s,%s,%s,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326),%s,%s,4,%s,%s)""",
                    (
                        dataset,
                        feature["source_kind"],
                        feature["source_id"],
                        feature["object_type"],
                        Jsonb(feature["geometry"]),
                        feature["min_zoom"],
                        feature["hidden"],
                        Jsonb(feature["style"]),
                        Jsonb(feature["properties"]),
                    ),
                )
                for layer in feature.get("layer_ids", []):
                    conn.execute(
                        "INSERT INTO map_data.feature_layers VALUES (%s,%s,%s,%s)",
                        (dataset, feature["source_kind"], feature["source_id"], layer),
                    )
            conn.execute(
                "UPDATE map_data.datasets SET status='ready' WHERE id=%s", (dataset,)
            )
    print(
        json.dumps({"datasetVersion": str(dataset), "features": len(data["features"])})
    )


def bake_task(dataset, format, z, x, y, root):
    start = time.monotonic()
    with nullcontext(_worker_conn) if _worker_conn is not None else connect() as conn:
        inputs = tile_inputs(
            conn, dataset, z, x, y, selection_zoom=selection_zoom(format, z)
        )
        folder = root / format / str(z) / str(x)
        folder.mkdir(parents=True, exist_ok=True)
        size = 0
        if format == "vector":
            payload = encode_mvt(conn, inputs, z, x, y)
            (folder / f"{y}.pbf").write_bytes(payload)
            size = len(payload)
        else:
            for density in (1, 2):
                payload = render_webp(inputs, z, x, y, density)
                (folder / f"{y}{'@2x' if density == 2 else ''}.webp").write_bytes(
                    payload
                )
                size += len(payload)
        (folder / f"{y}.inputs.json").write_text(
            json.dumps([f["attributes"]["objectId"] for f in inputs])
        )
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "seconds": time.monotonic() - start,
        "bytes": size,
        "inputCount": len(inputs),
        "rssMiB": peak / (1024**2 if sys.platform == "darwin" else 1024),
    }


def bake(args):
    started = time.monotonic()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    report = {
        "datasetVersion": str(args.dataset),
        "concurrency": args.concurrency,
        "zooms": [],
    }
    with connect() as conn:
        dataset = conn.execute(
            "SELECT status FROM map_data.datasets WHERE id=%s", (args.dataset,)
        ).fetchone()
        if not dataset or dataset["status"] != "ready":
            raise ValueError("Explicit ready dataset required")
        features = conn.execute(
            """SELECT source_kind,source_id,object_type,min_zoom,hidden,style,properties,
            ST_AsGeoJSON(geom)::jsonb AS geometry, COALESCE((SELECT array_agg(fl.layer_id)
            FROM map_data.feature_layers fl WHERE fl.dataset_id=f.dataset_id AND fl.source_kind=f.source_kind
            AND fl.source_id=f.source_id),'{}'::bigint[]) AS layer_ids FROM map_data.features f
            WHERE dataset_id=%s ORDER BY source_kind,source_id""",
            (args.dataset,),
        ).fetchall()
        (root / "features.json").write_text(json.dumps(features))
        for format in args.formats:
            maxzoom = args.vector_maxzoom if format == "vector" else args.raster_maxzoom
            for z in range(args.minzoom, maxzoom + 1):
                level_start = time.monotonic()
                plan = plan_zoom(conn, args.dataset, z, format=format)
                if args.bbox:
                    w, s, e, n = args.bbox
                    x0, y0 = xyz(w, n, z)
                    x1, y1 = xyz(e, s, z)
                    plan = [(x, y) for x, y in plan if x0 <= x <= x1 and y0 <= y <= y1]
                planned = time.monotonic()
                results = []
                batch_size = args.concurrency * TASKS_PER_PROCESS
                for offset in range(0, len(plan), batch_size):
                    # Explicit pool generations bound Cairo/Pillow RSS and avoid
                    # executor recycling deadlocks on older Python 3.12 builds.
                    with concurrent.futures.ProcessPoolExecutor(
                        max_workers=args.concurrency, initializer=init_worker
                    ) as pool:
                        tasks = [
                            pool.submit(bake_task, args.dataset, format, z, x, y, root)
                            for x, y in plan[offset : offset + batch_size]
                        ]
                        results.extend(task.result() for task in tasks)
                record = {
                    "format": format,
                    "z": z,
                    "tiles": len(plan),
                    "planSeconds": planned - level_start,
                    "seconds": time.monotonic() - level_start,
                    "bytes": sum(r["bytes"] for r in results),
                    "maxTileSeconds": max((r["seconds"] for r in results), default=0),
                    "maxWorkerRssMiB": max((r["rssMiB"] for r in results), default=0),
                }
                report["zooms"].append(record)
                print(json.dumps(record), flush=True)
    report["seconds"] = time.monotonic() - started
    report["canon"], report["canonHash"] = canon(
        max(args.vector_maxzoom, args.raster_maxzoom)
    )
    report.update(
        vectorMaxzoom=args.vector_maxzoom,
        rasterMaxzoom=args.raster_maxzoom,
        displayMaxzoom=DISPLAY_MAXZOOM,
    )
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")


def serve(args):
    root = args.output.resolve()
    report = json.loads((root / "report.json").read_text())
    features = json.loads((root / "features.json").read_text())
    render_features = []
    for feature in features:
        if feature["object_type"] == "cable_line" and visible_cable(feature, 24, [1]):
            render_features.append(
                {
                    "type": "Feature",
                    "id": f"{feature['source_kind']}:{feature['source_id']}",
                    "geometry": feature["geometry"],
                    "properties": render_attributes(feature),
                }
            )
    # Synthetic node overlapping the cable is intentional: checks cable below points.
    points = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [37, 55]},
                "properties": {"objectId": "node:fixture"},
            }
        ],
    }
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/audit-clear":
                requests.clear()
                self.respond(b"{}", "application/json")
            elif path == "/audit.json":
                self.respond(json.dumps(requests).encode(), "application/json")
            elif path == "/":
                self.respond(
                    (ROOT / "scripts/cable-tiles-prototype.html").read_bytes(),
                    "text/html",
                )
            elif path == "/metadata.json":
                self.respond(json.dumps(report).encode(), "application/json")
            elif path == "/objects.json":
                self.respond(
                    json.dumps(
                        {"type": "FeatureCollection", "features": render_features}
                    ).encode(),
                    "application/json",
                )
            elif path == "/points.json":
                self.respond(json.dumps(points).encode(), "application/json")
            elif path in ("/maplibre.js", "/maplibre.css", "/cable-style.js"):
                filename = {
                    "/maplibre.js": ROOT
                    / "frontend/node_modules/maplibre-gl/dist/maplibre-gl.mjs",
                    "/maplibre.css": ROOT
                    / "frontend/node_modules/maplibre-gl/dist/maplibre-gl.css",
                    "/cable-style.js": args.style.resolve(),
                }[path]
                self.respond(
                    filename.read_bytes(),
                    "text/css" if path.endswith(".css") else "application/javascript",
                )
            elif path in ("/maplibre-gl-shared.mjs", "/maplibre-gl-worker.mjs"):
                self.respond(
                    (
                        ROOT / "frontend/node_modules/maplibre-gl/dist" / path[1:]
                    ).read_bytes(),
                    "application/javascript",
                )
            elif path.startswith(("/vector/", "/raster/")):
                requests.append(path)
                parts = path.strip("/").split("/")
                if len(parts) != 4 or not all(p.isdigit() for p in parts[1:3]):
                    self.send_error(404)
                    return
                filename = root / path.lstrip("/")
                if ".." in parts[-1] or "/" in parts[-1]:
                    self.send_error(404)
                    return
                if path.endswith(".pbf"):
                    payload = filename.read_bytes() if filename.is_file() else EMPTY_MVT
                    self.respond(
                        gzip.compress(payload),
                        "application/vnd.mapbox-vector-tile",
                        gzip_encoded=True,
                    )
                elif path.endswith(".webp"):
                    density = 2 if path.endswith("@2x.webp") else 1
                    payload = (
                        filename.read_bytes()
                        if filename.is_file()
                        else render_webp([], 0, 0, 0, density)
                    )
                    self.respond(payload, "image/webp")
                else:
                    self.send_error(404)
            else:
                self.send_error(404)

        def respond(self, body, type, gzip_encoded=False):
            self.send_response(200)
            self.send_header("Content-Type", type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if gzip_encoded:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    print(f"Prototype at http://127.0.0.1:{args.port}", flush=True)

    class Server(ThreadingHTTPServer):
        request_queue_size = 128

    Server(("127.0.0.1", args.port), Handler).serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    importer = sub.add_parser("import")
    importer.add_argument("snapshot", type=Path)
    builder = sub.add_parser("bake")
    builder.add_argument("--dataset", type=UUID, required=True)
    builder.add_argument("--output", type=Path, required=True)
    builder.add_argument("--concurrency", type=int, default=CONCURRENCY)
    builder.add_argument("--minzoom", type=int, default=1)
    builder.add_argument("--vector-maxzoom", type=int, default=VECTOR_MAXZOOM)
    builder.add_argument("--raster-maxzoom", type=int, default=RASTER_PREBAKE_MAX)
    builder.add_argument(
        "--formats",
        nargs="+",
        choices=["vector", "raster"],
        default=["vector", "raster"],
    )
    builder.add_argument("--bbox", nargs=4, type=float)
    server = sub.add_parser("serve")
    server.add_argument("--output", type=Path, required=True)
    server.add_argument("--style", type=Path, required=True)
    server.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if args.command == "import":
        import_snapshot(args.snapshot)
    elif args.command == "bake":
        bake(args)
    else:
        serve(args)


if __name__ == "__main__":
    main()

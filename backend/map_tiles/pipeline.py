"""Full vector/raster prebake; publication is a separate lifecycle operation."""

import hashlib
import inspect
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg

from map_render import cables

from .defaults import TASKS_PER_PROCESS
from .geometry import canon
from .lifecycle import BuildHeartbeat, BuildSpec, TileError, TileLock
from .manifest import decode_vector, digest, validate_staging, write_json
from .planning import plan_zoom, selection_zoom
from .publication import cleanup_files
from .status_cache import VersionStatusCache
from .storage import md5
from .vector import encode_mvt, tile_inputs


def rules_hash(types):
    return hashlib.sha256(
        (inspect.getsource(cables) + json.dumps(list(types))).encode()
    ).hexdigest()


def bake_vector(config, dataset, *, progress=lambda record: None):
    return bake(config, dataset, "vector", progress=progress)


def bake_raster(config, dataset, *, progress=lambda record: None):
    return bake(config, dataset, "raster", progress=progress)


def bake(config, dataset, format, *, progress=lambda record: None):
    start = time.monotonic()
    cache = (
        VersionStatusCache.from_url(config.redis_url) if config.redis_url else VersionStatusCache()
    )
    build = None
    try:
        with (
            config.connect() as conn,
            TileLock(conn, config.map_id, format, cache=cache) as lease,
        ):
            lease.recover()
            cleanup_files(config, lease)
            # Fix a shared vector/raster tolerance table, even for vector z<=14.
            profile, tolerance_hash = canon(max(config.raster_prebake_max, config.vector_maxzoom))
            spec = BuildSpec(
                config.source_system,
                config.map_id,
                format,
                dataset,
                cables.RULES_VERSION,
                rules_hash(config.cable_type_ids),
                profile,
                tolerance_hash,
                config.minzoom,
                config.vector_maxzoom if format == "vector" else config.raster_prebake_max,
                config.display_maxzoom,
                {"cableTypeIds": list(config.cable_type_ids), "concurrency": config.concurrency},
            )
            build = lease.start(spec)
            folder = config.root / build.staging_path
            try:
                config.root.mkdir(parents=True, exist_ok=True)
                if config.root.is_symlink():
                    raise TileError("STAGING_UNSAFE", "Tile root cannot be a symlink")
                folder.parent.mkdir(parents=True, exist_ok=True)
                if not folder.resolve().is_relative_to(config.root.resolve()):
                    raise TileError("STAGING_UNSAFE", "Staging escaped the tile root")
                folder.mkdir(exist_ok=False)
                manifest = {
                    "tileVersion": str(build.version),
                    "datasetVersion": str(dataset),
                    "mapId": config.map_id,
                    "sourceSystem": config.source_system,
                    "format": format,
                    "rulesVersion": spec.rules_version,
                    "rulesHash": spec.rules_hash,
                    "minzoom": spec.minzoom,
                    "maxzoom": spec.maxzoom,
                    "displayMaxzoom": spec.display_maxzoom,
                    "geometryProfile": profile,
                    "toleranceHash": tolerance_hash,
                    "encoding": "raw-pbf" if format == "vector" else "webp",
                    "cableTypeIds": list(config.cable_type_ids),
                    "coverage": {
                        "native": {},
                        "overzoom": {"from": spec.maxzoom + 1, "to": spec.display_maxzoom},
                    },
                }
                if format == "raster":
                    manifest.update(prebakeMaxzoom=spec.maxzoom, densities=[1, 2], tileSize=256)
                bounds = conn.execute(
                    f"""SELECT ST_XMin(box) AS west,ST_YMin(box) AS south,
                    ST_XMax(box) AS east,ST_YMax(box) AS north FROM (
                    SELECT ST_Extent(geom)::box3d AS box FROM map_data.features f
                    WHERE dataset_id=%s AND object_type='cable_line'
                    AND source_kind=ANY(%s) AND {cables.visible_sql()}
                    AND {cables.cable_type_sql("f")}) selected""",
                    (
                        dataset,
                        list(cables.CABLE_SOURCE_KINDS),
                        selection_zoom(format, spec.maxzoom),
                        list(map(str, config.cable_type_ids)),
                        list(map(str, config.cable_type_ids)),
                    ),
                ).fetchone()
                manifest["bounds"] = list(bounds.values()) if bounds["west"] is not None else None
                report = {
                    "buildId": str(build.id),
                    "tileVersion": str(build.version),
                    "datasetVersion": str(dataset),
                    "format": format,
                    "concurrency": config.concurrency,
                    "zooms": [],
                }
                with BuildHeartbeat(lease, build, config.connect) as monitor:

                    def check():
                        monitor.check()
                        lease.check()

                    for z in range(spec.minzoom, spec.maxzoom + 1):
                        check()
                        before = time.monotonic()
                        plan = plan_zoom(
                            conn, dataset, z, format=format, cable_type_ids=config.cable_type_ids
                        )
                        entries = []
                        batch_size = config.concurrency * TASKS_PER_PROCESS
                        if format == "raster":
                            from .raster_worker import render_batches

                            entries = render_batches(config, dataset, z, plan, folder, check)
                        else:
                            for offset in range(0, len(plan), batch_size):
                                check()
                                connections = []
                                local = threading.local()
                                cancelled = threading.Event()

                                def initialize(local=local, connections=connections):
                                    local.conn = config.connect()
                                    connections.append(local.conn)

                                def render(xy, local=local, z=z, cancelled=cancelled):
                                    if cancelled.is_set():
                                        raise TileError(
                                            "BUILD_CANCELLED", "Worker batch was cancelled"
                                        )
                                    monitor.check()
                                    x, y = xy
                                    inputs = tile_inputs(
                                        local.conn, dataset, z, x, y, config.cable_type_ids
                                    )
                                    payload = encode_mvt(local.conn, inputs, z, x, y)
                                    # Validate parity before writing; rechecked from disk below.
                                    decoded = decode_vector(payload)
                                    ids = sorted(f["attributes"]["objectId"] for f in inputs)
                                    if sorted(f["properties"]["objectId"] for f in decoded) != ids:
                                        raise TileError(
                                            "TILE_INVALID", "Encoder lost selected cable IDs"
                                        )
                                    relative = f"{z}/{x}/{y}.pbf"
                                    path = folder / relative
                                    path.parent.mkdir(parents=True, exist_ok=True)
                                    if cancelled.is_set():
                                        raise TileError(
                                            "BUILD_CANCELLED", "Worker batch was cancelled"
                                        )
                                    path.write_bytes(payload)
                                    return {
                                        "x": x,
                                        "y": y,
                                        "path": relative,
                                        "status": "prebaked",
                                        "bytes": len(payload),
                                        "sha256": digest(payload),
                                        "md5": md5(payload),
                                        "objectIds": ids,
                                    }

                                pool = ThreadPoolExecutor(
                                    max_workers=config.concurrency, initializer=initialize
                                )
                                try:
                                    entries.extend(
                                        pool.map(render, plan[offset : offset + batch_size])
                                    )
                                except BaseException:
                                    cancelled.set()
                                    raise
                                finally:
                                    pool.shutdown(wait=True, cancel_futures=True)
                                    for connection in connections:
                                        connection.close()
                        manifest["coverage"]["native"][str(z)] = entries
                        record = {
                            "z": z,
                            "tiles": len(entries),
                            "bytes": sum(e["bytes"] for e in entries),
                            "peakWorkerRssMiB": max(
                                (e.get("peakWorkerRssMiB", 0) for e in entries), default=0
                            ),
                            "seconds": time.monotonic() - before,
                        }
                        report["zooms"].append(record)
                        progress(record)
                    report["seconds"] = time.monotonic() - start
                    write_json(folder / "manifest.json", manifest)
                    write_json(folder / "report.json", report)
                    validation_start = time.monotonic()
                    validate_staging(folder, manifest, check)
                    report["validationSeconds"] = time.monotonic() - validation_start
                    report["seconds"] = time.monotonic() - start
                    write_json(folder / "report.json", report)
                    lease.validated(build, manifest)
                lease.finish(build)
                cleanup_report = cleanup_files(config, lease)
                result = {
                    **report,
                    "status": "validated",
                    "stagingPath": build.staging_path,
                    "cleanup": cleanup_report,
                }
                return result
            except BaseException as error:
                # SIGINT is a failed build with recoverable pending file cleanup.
                # If the lease died, recovery must handle it; never write unfenced.
                try:
                    lease.fail(
                        build,
                        "BUILD_CANCELLED"
                        if isinstance(error, KeyboardInterrupt)
                        else getattr(error, "code", "BUILD_FAILED"),
                    )
                    cleanup_files(config, lease)
                except (TileError, psycopg.Error):
                    error.add_note(
                        "Build state could not be finalized; locked recovery is required"
                    )
                raise
    finally:
        cache.close()

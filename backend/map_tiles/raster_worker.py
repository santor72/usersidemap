"""Spawned Cairo workers, explicitly recycled after bounded batches."""

import multiprocessing
import resource
import sys
from concurrent.futures import ProcessPoolExecutor, TimeoutError

from .lifecycle import TileError
from .manifest import digest, validate_webp
from .planning import selection_zoom
from .storage import md5
from .vector import tile_inputs

_config = _cancelled = _conn = None


def initialize(config, cancelled):
    global _config, _cancelled, _conn
    _config, _cancelled, _conn = config, cancelled, None


def render(task):
    global _conn
    from .raster import render_webp

    dataset, z, x, y, folder = task
    try:
        if _cancelled.is_set():
            raise TileError("BUILD_CANCELLED", "Raster batch cancelled")
        if _conn is None:
            _conn = _config.connect()
        inputs = tile_inputs(
            _conn,
            dataset,
            z,
            x,
            y,
            _config.cable_type_ids,
            selection_zoom=selection_zoom("raster", z),
        )
        files = {}
        for density in (1, 2):
            payload = render_webp(inputs, z, x, y, density)
            validate_webp(payload, density)
            relative = f"{z}/{x}/{y}{'@2x' if density == 2 else ''}.webp"
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if _cancelled.is_set():
                raise TileError("BUILD_CANCELLED", "Raster batch cancelled")
            path.write_bytes(payload)
            files[str(density)] = {
                "path": relative,
                "bytes": len(payload),
                "sha256": digest(payload),
                "md5": md5(payload),
            }
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {
            "x": x,
            "y": y,
            "status": "prebaked",
            "objectIds": sorted(f["attributes"]["objectId"] for f in inputs),
            "files": files,
            "bytes": sum(f["bytes"] for f in files.values()),
            "peakWorkerRssMiB": peak / (1024**2 if sys.platform == "darwin" else 1024),
        }
    except TileError:
        raise
    except Exception as error:  # noqa: BLE001 -- do not transmit DB/file secrets across processes
        raise TileError(
            "RASTER_WORKER_FAILED", f"Raster worker failed ({type(error).__name__})"
        ) from None


def render_batches(config, dataset, z, plan, folder, check):
    context = multiprocessing.get_context("spawn")
    entries = []
    size = config.concurrency * config.tasks_per_process
    for offset in range(0, len(plan), size):
        check()
        cancelled = context.Event()
        pool = ProcessPoolExecutor(
            max_workers=config.concurrency,
            mp_context=context,
            initializer=initialize,
            initargs=(config, cancelled),
        )
        futures = [
            pool.submit(render, (dataset, z, x, y, folder)) for x, y in plan[offset : offset + size]
        ]
        try:
            for future in futures:
                while True:
                    check()
                    try:
                        entries.append(future.result(timeout=0.2))
                        break
                    except TimeoutError:
                        continue
        except BaseException:
            cancelled.set()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
    return entries

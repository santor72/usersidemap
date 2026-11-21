"""Concurrent first tile requests share one large manifest download/decoding."""

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from uuid import uuid4

from map_api import tile_routes
from map_api.tile_settings import TileSettings


def test_concurrent_first_tiles_load_one_manifest(tmp_path, monkeypatch):
    version = str(uuid4())
    row = {"id": version, "map_id": 3, "format": "vector", "final_path": f"3/vector/{version}"}
    payload = b"validated-tile"
    path = tmp_path / row["final_path"] / "1/1/1.pbf"
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    manifest = {"tileVersion": version, "mapId": 3, "format": "vector", "minzoom": 1, "maxzoom": 1}
    entry = {
        "path": "1/1/1.pbf",
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    ready = threading.Barrier(16)
    started, release = threading.Event(), threading.Event()
    calls = []

    @lru_cache(maxsize=4)
    def slow_index(*args):
        calls.append(args)
        started.set()
        assert release.wait(5)
        return manifest, {(1, 1, 1): entry}

    monkeypatch.setattr(tile_routes, "file_index", slow_index)

    def request():
        ready.wait(timeout=5)
        return tile_routes.tile_payload(TileSettings(tmp_path), row, 1, 1, 1, 1)

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(request) for _ in range(16)]
        try:
            assert started.wait(5)
        finally:
            release.set()
        assert [f.result(timeout=5) for f in futures] == [payload] * 16
    assert len(calls) == 1

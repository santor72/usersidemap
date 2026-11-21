"""Real SeaweedFS integrity and lifecycle gates; isolated disposable bucket/DB."""

import json
import os
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from test_cable_render_database import published_fixture  # noqa: F401
from test_vector_pipeline import tile_fixture as tile_fixture  # noqa: PLC0414

from map_api.tile_routes import file_index, tile_payload
from map_api.tile_settings import TileSettings
from map_tiles.lifecycle import TileError, TileLock
from map_tiles.manifest import validate_staging
from map_tiles.pipeline import bake_raster, bake_vector
from map_tiles.publication import cleanup_files, publish_locked, rollback_locked, safe_path
from map_tiles.storage import S3Path, S3Settings


@pytest.fixture
def s3_root():
    endpoint = os.environ.get("TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("TEST_S3_ENDPOINT must point to disposable SeaweedFS")
    return S3Path(S3Settings(endpoint, "map-test", "map-test", "map-test-secret"))


def test_storage_prefix_promotion_and_delete(s3_root):
    staging = s3_root / f"3/vector/.staging/{uuid4()}"
    final = s3_root / staging.object_key
    try:
        staging.mkdir()
        tile = staging / "14/1/2.pbf"
        tile.write_bytes(b"payload")
        assert tile.read_bytes() == b"payload"
        assert staging.exists() and not final.exists()
        assert set(staging.inventory()) == {"14/1/2.pbf"}
        staging.rename(final)
        assert not staging.exists() and final.exists()
        assert (final / "14/1/2.pbf").read_bytes() == b"payload"
        for unsafe in ["/other", "../other", "3/vector/../../other"]:
            with pytest.raises(TileError):
                safe_path(s3_root, unsafe)
        with pytest.raises(TileError):
            (s3_root / "3/vector").remove()
        assert (final / "14/1/2.pbf").read_bytes() == b"payload"
    finally:
        staging.remove()
    assert not final.exists() and not staging.inventory()


@pytest.mark.parametrize("format", ["vector", "raster"])
def test_s3_pipeline_publish_corruption_rollback_gc(tile_fixture, s3_root, format):
    conn, dataset, config = tile_fixture
    config = replace(config, root=s3_root, minzoom=14)
    bake = bake_vector if format == "vector" else bake_raster
    versions = []
    try:
        for _ in range(3):
            result = bake(config, dataset)
            version = UUID(result["tileVersion"])
            versions.append(version)
            folder = s3_root / result["stagingPath"]
            manifest = json.loads((folder / "manifest.json").read_text())
            entry = manifest["coverage"]["native"]["14"][0]
            record = entry if format == "vector" else entry["files"]["1"]
            original = (folder / record["path"]).read_bytes()
            # List ETags must reject corrupt objects before active pointer changes.
            (folder / record["path"]).write_bytes(b"corrupt")
            with pytest.raises(TileError, match="checksum"):
                validate_staging(folder, manifest)
            (folder / record["path"]).write_bytes(original)
            with TileLock(conn, 3, format) as lease:
                published = publish_locked(config, lease, version)
                assert published["published"]
            row = conn.execute(
                "SELECT * FROM map_data.tile_versions WHERE id=%s", (version,)
            ).fetchone()
            row["id"] = str(row["id"])
            assert (
                tile_payload(TileSettings(s3_root), row, 14, entry["x"], entry["y"], 1) == original
            )
        with TileLock(conn, 3, format) as lease:
            rollback = rollback_locked(config, lease)
            assert rollback["version"] == str(versions[1])
            assert not cleanup_files(config, lease)["cleanupPending"]
        assert not (s3_root / f"3/{format}/{versions[0]}").exists()
        assert (s3_root / f"3/{format}/{versions[1]}").exists()
        assert (s3_root / f"3/{format}/{versions[2]}").exists()
    finally:
        file_index.cache_clear()
        for version in versions:
            (s3_root / f"3/{format}/{version}").remove()

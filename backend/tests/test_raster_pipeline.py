"""Full raster prebake/retina/integrity and independent format lifecycle."""

import io
import json
import multiprocessing
from dataclasses import replace

import pytest
from PIL import Image
from test_cable_render_database import published_fixture  # noqa: F401 -- fixture dependency
from test_vector_pipeline import (
    tile_fixture as tile_fixture,  # noqa: PLC0414 -- pytest fixture re-export
)

from map_tiles.lifecycle import TileError
from map_tiles.manifest import digest, validate_staging, write_json
from map_tiles.pipeline import bake_raster, bake_vector


def test_full_raster_both_densities_and_shared_vector_rules(tile_fixture):
    conn, dataset, config = tile_fixture
    raster = bake_raster(config, dataset)
    vector = bake_vector(config, dataset)
    folder = config.root / raster["stagingPath"]
    manifest = json.loads((folder / "manifest.json").read_text())
    vm = json.loads((config.root / vector["stagingPath"] / "manifest.json").read_text())
    assert raster["status"] == "validated"
    assert {r["z"] for r in raster["zooms"]} == set(range(1, 16))
    assert manifest["encoding"] == "webp" and manifest["densities"] == [1, 2]
    assert manifest["prebakeMaxzoom"] == 15
    assert manifest["coverage"]["overzoom"] == {"from": 16, "to": 18}
    assert manifest["toleranceHash"] == vm["toleranceHash"]
    assert manifest["rulesHash"] == vm["rulesHash"]
    raster_ids = {i for tile in manifest["coverage"]["native"]["15"] for i in tile["objectIds"]}
    vector_ids = {i for tile in vm["coverage"]["native"]["14"] for i in tile["objectIds"]}
    assert raster_ids == vector_ids
    assert "cable:1" in raster_ids and "cable_group:5" in raster_ids
    assert validate_staging(folder, manifest) == 2 * sum(r["tiles"] for r in raster["zooms"])
    assert not multiprocessing.active_children()
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM map_data.active_tile_versions WHERE source_system=%s",
            (config.source_system,),
        ).fetchone()["n"]
        == 0
    )
    entry = manifest["coverage"]["native"]["15"][0]
    for density in (1, 2):
        data = (folder / entry["files"][str(density)]["path"]).read_bytes()
        with Image.open(io.BytesIO(data)) as image:
            assert image.size == (256 * density, 256 * density)
            assert image.convert("RGBA").getextrema()[3][0] == 0
    target = folder / entry["files"]["2"]["path"]
    original = target.read_bytes()
    target.unlink()
    with pytest.raises(TileError, match="missing"):
        validate_staging(folder, manifest)
    target.write_bytes(b"corrupt")
    with pytest.raises(TileError, match="checksum"):
        validate_staging(folder, manifest)
    # Correct hash cannot make corrupt bytes pass independent WebP decoding.
    entry["files"]["2"].update(bytes=7, sha256=digest(b"corrupt"))
    write_json(folder / "manifest.json", manifest)
    with pytest.raises(TileError, match="decoding"):
        validate_staging(folder, manifest)
    target.write_bytes(original)


def test_raster_cancel_does_not_block_vector(tile_fixture, monkeypatch):
    conn, dataset, config = tile_fixture

    from map_tiles import publication

    def blocked(path):
        raise OSError("Injected file GC failure")

    monkeypatch.setattr(publication, "remove_tree", blocked)

    def cancel(record):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        bake_raster(config, dataset, progress=cancel)
    assert not multiprocessing.active_children()
    with pytest.raises(TileError) as error:
        bake_raster(config, dataset)
    assert error.value.code == "CLEANUP_PENDING"
    assert bake_vector(config, dataset)["status"] == "validated"
    row = conn.execute(
        "SELECT status,error_code FROM map_data.tile_builds WHERE source_system=%s AND format='raster'",
        (config.source_system,),
    ).fetchone()
    assert row == {"status": "failed", "error_code": "BUILD_CANCELLED"}


def test_raster_native_minimum_and_worker_error_pickle(tile_fixture):
    import pickle

    _, _, config = tile_fixture
    with pytest.raises(ValueError, match=">=15"):
        replace(config, raster_prebake_max=14)
    error = pickle.loads(pickle.dumps(TileError("TILE_INVALID", "safe")))
    assert error.code == "TILE_INVALID" and str(error) == "safe"


@pytest.mark.parametrize("tile_fixture", ["invalid-color"], indirect=True)
def test_spawned_worker_failure_is_safe_and_stops_pool(tile_fixture):
    conn, dataset, config = tile_fixture
    with pytest.raises(TileError) as failure:
        bake_raster(replace(config, minzoom=14), dataset)
    assert failure.value.code == "RASTER_WORKER_FAILED"
    assert str(failure.value) == "Raster worker failed (ValueError)"
    assert not multiprocessing.active_children()
    assert conn.execute(
        "SELECT status,error_code FROM map_data.tile_builds WHERE source_system=%s",
        (config.source_system,),
    ).fetchone() == {"status": "failed", "error_code": "RASTER_WORKER_FAILED"}

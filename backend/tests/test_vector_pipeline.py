"""Full native staging bake on disposable PostGIS, with actual file validation."""

import copy
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
import test_cable_render_database
from test_cable_render_database import published_fixture  # noqa: F401 -- expose fixture

from map_tiles.config import TileConfig
from map_tiles.lifecycle import TileError
from map_tiles.manifest import decode_vector, validate_staging
from map_tiles.pipeline import bake_vector


@pytest.fixture
def tile_fixture(request, monkeypatch, tmp_path):
    fixture = copy.deepcopy(test_cable_render_database.FIXTURE)
    # The antimeridian fixture intentionally encodes a world-spanning planar line;
    # its edge behavior has separate tests. Avoid 32768 high-zoom fixture tiles.
    fixture["features"] = [f for f in fixture["features"] if f["source_id"] != 8]
    if getattr(request, "param", None) == "invalid-color":
        fixture["features"][1]["style"]["color"] = "invalid-color-fixture"
    if getattr(request, "param", None) == "with-node":
        fixture["features"].append(
            {
                "source_kind": "node",
                "source_id": 100,
                "object_type": "node",
                "geometry": {"type": "Point", "coordinates": [37, 55]},
                "min_zoom": 1,
                "hidden": False,
                "style": {},
                "properties": {},
                "layer_ids": [1],
            }
        )
    monkeypatch.setattr(test_cable_render_database, "FIXTURE", fixture)
    conn, dataset, source, url = request.getfixturevalue("published_fixture")
    config = TileConfig(url, tmp_path / "tiles", source_system=source)
    try:
        yield conn, dataset, config
    finally:
        with conn.transaction():
            conn.execute(
                "DELETE FROM map_data.active_tile_versions WHERE source_system=%s", (source,)
            )
            conn.execute("DELETE FROM map_data.tile_versions WHERE source_system=%s", (source,))
            conn.execute("DELETE FROM map_data.tile_builds WHERE source_system=%s", (source,))


def test_full_vector_manifest_parity_and_missing_corrupt_gate(tile_fixture):
    conn, dataset, config = tile_fixture
    result = bake_vector(config, dataset)
    folder = config.root / result["stagingPath"]
    manifest = json.loads((folder / "manifest.json").read_text())
    assert result["status"] == "validated"
    assert {record["z"] for record in result["zooms"]} == set(range(1, 15))
    assert manifest["encoding"] == "raw-pbf"
    assert manifest["coverage"]["overzoom"] == {"from": 15, "to": 18}
    assert manifest["geometryProfile"]["tolerances"]["14"] == 0
    assert manifest["bounds"][2] < 180
    assert validate_staging(folder, manifest) == sum(r["tiles"] for r in result["zooms"])
    ids = {
        identifier
        for tile in manifest["coverage"]["native"]["14"]
        for identifier in tile["objectIds"]
    }
    assert ids == {"cable:1", "cable:2", "cable:3", "cable_group:5", "cable:7", "cable:9"}
    props = [
        f["properties"]
        for tile in manifest["coverage"]["native"]["14"]
        for f in decode_vector((folder / tile["path"]).read_bytes())
    ]
    assert any(p["layerIds"] == ",1,11," for p in props)
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM map_data.active_tile_versions WHERE source_system=%s",
            (config.source_system,),
        ).fetchone()["n"]
        == 0
    )
    assert (
        conn.execute(
            "SELECT status FROM map_data.tile_builds WHERE id=%s", (result["buildId"],)
        ).fetchone()["status"]
        == "succeeded"
    )
    entry = manifest["coverage"]["native"]["14"][0]
    path = folder / entry["path"]
    original = path.read_bytes()
    path.write_bytes(b"corrupt")
    with pytest.raises(TileError, match="checksum"):
        validate_staging(folder, manifest)
    path.unlink()
    with pytest.raises(TileError, match="missing"):
        validate_staging(folder, manifest)
    path.write_bytes(original)
    assert validate_staging(folder, manifest) > 0
    (folder / "unexpected.txt").write_text("unexpected")
    with pytest.raises(TileError, match="unexpected"):
        validate_staging(folder, manifest)
    (folder / "unexpected.txt").unlink()


def test_explicit_dataset_rejected_before_staging(tile_fixture):
    conn, dataset, config = tile_fixture
    for wrong_config, wrong_dataset in (
        (config, uuid4()),
        (replace(config, source_system="other"), dataset),
    ):
        with pytest.raises(TileError) as failure:
            bake_vector(wrong_config, wrong_dataset)
        assert failure.value.code == "DATASET_UNAVAILABLE"
    assert not config.root.exists()
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM map_data.tile_builds WHERE source_system=%s",
            (config.source_system,),
        ).fetchone()["n"]
        == 0
    )


def test_cancel_retains_pending_staging_and_blocks_retry(tile_fixture, monkeypatch):
    conn, dataset, config = tile_fixture

    from map_tiles import publication

    remove = publication.remove_tree

    def blocked(path):
        raise OSError("Injected file GC failure")

    monkeypatch.setattr(publication, "remove_tree", blocked)

    def cancel(record):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        bake_vector(config, dataset, progress=cancel)
    build = conn.execute(
        "SELECT * FROM map_data.tile_builds WHERE source_system=%s", (config.source_system,)
    ).fetchone()
    assert build["status"] == "failed" and build["error_code"] == "BUILD_CANCELLED"
    version = conn.execute(
        "SELECT * FROM map_data.tile_versions WHERE id=%s", (build["version_id"],)
    ).fetchone()
    assert version["status"] == "failed" and version["cleanup_status"] == "error"
    assert (config.root / version["staging_path"]).is_dir()
    with pytest.raises(TileError) as error:
        bake_vector(config, dataset)
    assert error.value.code == "CLEANUP_PENDING"
    monkeypatch.setattr(publication, "remove_tree", remove)
    # Startup cleanup retries the failed GC, then a new UUID is allowed.
    retry = bake_vector(config, dataset)
    assert retry["tileVersion"] != str(version["id"])
    assert retry["status"] == "validated"
    assert not (config.root / version["staging_path"]).exists()
    assert (
        conn.execute(
            "SELECT status FROM map_data.tile_versions WHERE id=%s", (version["id"],)
        ).fetchone()["status"]
        == "deleted"
    )


def test_config_and_cli_require_explicit_dataset(monkeypatch):
    from map_tiles.cli import main

    with pytest.raises(SystemExit) as failure:
        main(["vector"])
    assert failure.value.code == 2
    with pytest.raises(ValueError):
        TileConfig("", Path("tiles"))
    with pytest.raises(ValueError):
        TileConfig("postgresql://unused", Path("tiles"), vector_maxzoom=13)


def test_worker_failure_closes_batch_and_records_safe_code(tile_fixture, monkeypatch):
    import threading

    from map_tiles import pipeline

    conn, dataset, config = tile_fixture

    def fail(*args, **kwargs):
        raise TileError("TILE_INVALID", "Injected encoder failure")

    monkeypatch.setattr(pipeline, "encode_mvt", fail)
    with pytest.raises(TileError, match="Injected"):
        bake_vector(replace(config, minzoom=14), dataset)
    row = conn.execute(
        "SELECT status,error_code FROM map_data.tile_builds WHERE source_system=%s",
        (config.source_system,),
    ).fetchone()
    assert row == {"status": "failed", "error_code": "TILE_INVALID"}
    assert not any(
        t.name.startswith("ThreadPoolExecutor") or t.name.startswith("tile-heartbeat")
        for t in threading.enumerate()
    )

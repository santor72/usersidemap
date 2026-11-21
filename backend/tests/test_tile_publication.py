"""Real filesystem rename/commit/GC gates, retained pointers and rollback."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from test_cable_render_database import published_fixture  # noqa: F401 -- fixture dependency
from test_vector_pipeline import tile_fixture as tile_fixture  # noqa: PLC0414 -- pytest re-export

from map_tiles.lifecycle import TileError, TileLock
from map_tiles.pipeline import bake_raster, bake_vector
from map_tiles.publication import cleanup_files, publish_locked, rollback_locked, safe_path


def release(config, dataset, format="vector"):
    return (bake_vector if format == "vector" else bake_raster)(
        replace(config, minzoom=14), dataset
    )


def pointers(conn, config, format="vector"):
    return conn.execute(
        """SELECT active_version,prev_version FROM map_data.active_tile_versions
        WHERE source_system=%s AND map_id=%s AND format=%s""",
        (config.source_system, config.map_id, format),
    ).fetchone()


def crash_at(target):
    def inject(step):
        if step == target:
            raise OSError("Injected " + step)

    return inject


@pytest.mark.parametrize("format", ["vector", "raster"])
def test_publish_retains_two_gc_and_rollback_dataset(tile_fixture, format):
    conn, dataset, config = tile_fixture
    versions = []
    # Another ready snapshot gives rollback an observable provenance change.
    run, other = uuid4(), uuid4()
    conn.execute(
        "INSERT INTO map_data.sync_runs (id,source_system,map_id,status,rules_version) VALUES (%s,%s,3,'succeeded','fixture')",
        (run, config.source_system),
    )
    conn.execute(
        "INSERT INTO map_data.datasets (id,run_id,source_system,map_id,status) VALUES (%s,%s,%s,3,'building')",
        (other, run, config.source_system),
    )
    conn.execute(
        "INSERT INTO map_data.layers (dataset_id,source_id,title,default_visible) SELECT %s,source_id,title,default_visible FROM map_data.layers WHERE dataset_id=%s",
        (other, dataset),
    )
    conn.execute(
        "INSERT INTO map_data.features (dataset_id,source_kind,source_id,object_type,geom,min_zoom,hidden,vertex_count,style,properties) SELECT %s,source_kind,source_id,object_type,geom,min_zoom,hidden,vertex_count,style,properties FROM map_data.features WHERE dataset_id=%s",
        (other, dataset),
    )
    conn.execute(
        "INSERT INTO map_data.feature_layers SELECT %s,source_kind,source_id,layer_id FROM map_data.feature_layers WHERE dataset_id=%s",
        (other, dataset),
    )
    conn.execute("UPDATE map_data.datasets SET status='ready' WHERE id=%s", (other,))
    try:
        for ds in (dataset, other, dataset):
            report = release(config, ds, format)
            version = UUID(report["tileVersion"])
            versions.append(version)
            with TileLock(conn, 3, format) as lease:
                result = publish_locked(config, lease, version)
                assert result["published"] and not result["cleanup"]["cleanupPending"]
        assert pointers(conn, config, format) == {
            "active_version": versions[2],
            "prev_version": versions[1],
        }
        assert conn.execute(
            "SELECT status,cleanup_status FROM map_data.tile_versions WHERE id=%s", (versions[0],)
        ).fetchone() == {"status": "deleted", "cleanup_status": "done"}
        assert not (config.root / f"3/{format}/{versions[0]}").exists()
        kept = list((config.root / f"3/{format}").glob("*"))
        assert {p.name for p in kept if p.name != ".staging"} == {
            str(versions[1]),
            str(versions[2]),
        }
        before = {
            p.relative_to(config.root).as_posix(): p.read_bytes()
            for p in (config.root / f"3/{format}" / str(versions[1])).rglob("*")
            if p.is_file()
        }
        with TileLock(conn, 3, format) as lease:
            result = rollback_locked(config, lease, target=versions[1])
            assert rollback_locked(config, lease, target=versions[1])["alreadyActive"]
            assert result["datasetVersion"] == str(other)
            assert pointers(conn, config, format)["active_version"] == versions[1]
            with pytest.raises(TileError) as error:
                publish_locked(config, lease, versions[2])
            assert error.value.code == "PUBLISH_STATE_INVALID"
        after = {
            p.relative_to(config.root).as_posix(): p.read_bytes()
            for p in (config.root / f"3/{format}" / str(versions[1])).rglob("*")
            if p.is_file()
        }
        assert after == before
    finally:
        # fixture teardown removes all tile references before datasets.
        # Save this extra run for cleanup after those references are removed.
        with conn.transaction():
            conn.execute(
                "DELETE FROM map_data.active_tile_versions WHERE source_system=%s",
                (config.source_system,),
            )
            conn.execute(
                "DELETE FROM map_data.tile_versions WHERE source_system=%s", (config.source_system,)
            )
            conn.execute(
                "DELETE FROM map_data.tile_builds WHERE source_system=%s", (config.source_system,)
            )
            conn.execute("DELETE FROM map_data.datasets WHERE id=%s", (other,))
            conn.execute("DELETE FROM map_data.sync_runs WHERE id=%s", (run,))


@pytest.mark.parametrize("step", ["before_rename", "after_rename", "before_commit", "after_commit"])
def test_publish_crash_retry_never_exposes_partial_release(tile_fixture, step):
    conn, dataset, config = tile_fixture
    first = release(config, dataset)
    second = release(config, dataset)
    v1, v2 = UUID(first["tileVersion"]), UUID(second["tileVersion"])
    with TileLock(conn, 3, "vector") as lease:
        publish_locked(config, lease, v1)
        with pytest.raises(OSError):
            publish_locked(config, lease, v2, fault=crash_at(step))
        assert pointers(conn, config)["active_version"] == (v2 if step == "after_commit" else v1)
        status = conn.execute(
            "SELECT status FROM map_data.tile_versions WHERE id=%s", (v2,)
        ).fetchone()["status"]
        assert status == ("ready" if step == "after_commit" else "validated")
        result = publish_locked(config, lease, v2)
        assert result["published"]
        assert pointers(conn, config) == {"active_version": v2, "prev_version": v1}
        assert not (config.root / second["stagingPath"]).exists()
        assert (config.root / f"3/vector/{v2}/manifest.json").is_file()


def test_gc_failure_publish_success_format_independence_and_retry(tile_fixture, monkeypatch):
    conn, dataset, config = tile_fixture
    vector = [release(config, dataset) for _ in range(3)]
    with TileLock(conn, 3, "vector") as lease:
        for r in vector[:2]:
            publish_locked(config, lease, UUID(r["tileVersion"]))
        result = publish_locked(
            config, lease, UUID(vector[2]["tileVersion"]), fault=crash_at("before_delete")
        )
        assert result["published"] and result["cleanup"]["cleanupPending"]
        retired = UUID(vector[0]["tileVersion"])
        row = conn.execute(
            "SELECT status,cleanup_status,cleanup_error_code FROM map_data.tile_versions WHERE id=%s",
            (retired,),
        ).fetchone()
        assert row == {
            "status": "retired",
            "cleanup_status": "error",
            "cleanup_error_code": "FILE_CLEANUP_FAILED",
        }
        # Direct start is blocked until GC; CLI/pipeline startup attempts it.
        with pytest.raises(TileError, match="cleanup"):
            cleanup_files(config, lease, fault=crash_at("before_delete"))
    from map_tiles import publication

    def blocked(path):
        raise OSError("Injected persistent GC failure")

    with monkeypatch.context() as context, TileLock(conn, 3, "vector") as lease:
        context.setattr(publication, "remove_tree", blocked)
        rollback = rollback_locked(config, lease)
        assert rollback["rolledBack"] and rollback["cleanup"]["cleanupPending"]
        assert pointers(conn, config)["active_version"] == UUID(vector[1]["tileVersion"])
    raster = release(config, dataset, "raster")
    with TileLock(conn, 3, "raster") as lease:
        assert publish_locked(config, lease, UUID(raster["tileVersion"]))["published"]
    with TileLock(conn, 3, "vector") as lease:
        assert cleanup_files(config, lease)["deleted"] == [str(retired)]
        assert cleanup_files(config, lease)["deleted"] == []
        assert not (config.root / f"3/vector/{retired}").exists()


def test_delete_before_status_commit_is_idempotent(tile_fixture):
    conn, dataset, config = tile_fixture
    r = release(config, dataset)
    version = UUID(r["tileVersion"])
    conn.execute(
        "UPDATE map_data.tile_versions SET status='failed',cleanup_status='pending' WHERE id=%s",
        (version,),
    )
    with TileLock(conn, 3, "vector") as lease:
        with pytest.raises(TileError):
            cleanup_files(config, lease, fault=crash_at("after_delete"))
        assert not (config.root / r["stagingPath"]).exists()
        assert cleanup_files(config, lease)["deleted"] == [str(version)]
        assert cleanup_files(config, lease)["deleted"] == []


def test_general_cleanup_discards_unpublished_final_but_not_staging(tile_fixture):
    conn, dataset, config = tile_fixture
    r = release(config, dataset)
    waiting = release(config, dataset)
    version = UUID(r["tileVersion"])
    with TileLock(conn, 3, "vector") as lease:
        with pytest.raises(OSError):
            publish_locked(config, lease, version, fault=crash_at("after_rename"))
        assert cleanup_files(config, lease)["deleted"] == [str(version)]
        assert (config.root / waiting["stagingPath"]).is_dir()
        assert pointers(conn, config) is None


def test_missing_previous_and_symlink_cannot_destroy_retained_files(tile_fixture, tmp_path):
    conn, dataset, config = tile_fixture
    a, b = release(config, dataset), release(config, dataset)
    with TileLock(conn, 3, "vector") as lease:
        publish_locked(config, lease, UUID(a["tileVersion"]))
        publish_locked(config, lease, UUID(b["tileVersion"]))
        previous = config.root / f"3/vector/{a['tileVersion']}"
        (previous / "manifest.json").unlink()
        with pytest.raises((TileError, FileNotFoundError)):
            rollback_locked(config, lease)
        assert pointers(conn, config)["active_version"] == UUID(b["tileVersion"])
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep").write_text("safe")
    unsafe = config.root / "3/vector/unsafe"
    unsafe.symlink_to(external, target_is_directory=True)
    with pytest.raises(TileError):
        safe_path(config.root, "3/vector/unsafe")
    assert (external / "keep").read_text() == "safe"


@pytest.mark.parametrize("format", ["vector", "raster"])
def test_real_cache_invalidation_and_worker_publish_grants(tile_fixture, format):
    import os

    from map_sync.grants import grant_tile_roles
    from map_tiles.status_cache import VersionStatusCache, status_key

    conn, dataset, config = tile_fixture
    url = os.environ.get("TEST_MAP_REDIS_URL")
    if not url:
        pytest.skip("Disposable Redis required")
    cache = VersionStatusCache.from_url(url)
    first, second, third = (release(config, dataset, format) for _ in range(3))
    v1, v2, v3 = (UUID(r["tileVersion"]) for r in (first, second, third))
    grant_tile_roles(conn)
    conn.execute("SET ROLE map_tiles_" + format)
    try:
        with TileLock(conn, 3, format, cache=cache) as lease:
            assert (
                conn.execute("SELECT current_user AS role").fetchone()["role"]
                == "map_tiles_" + format
            )
            publish_locked(config, lease, v1)
            assert cache.resolve(conn, v1)["status"] == "ready"
            publish_locked(config, lease, v2)
            assert cache.client.get(status_key(v1)) is None
            cache.resolve(conn, v1)
            cache.resolve(conn, v2)
            rollback_locked(config, lease)
            assert cache.client.get(status_key(v1)) is None
            assert cache.client.get(status_key(v2)) is None
            cache.resolve(conn, v1)
            cache.resolve(conn, v2)
            publish_locked(config, lease, v3)
            assert cache.client.get(status_key(v1)) is None
            assert cache.client.get(status_key(v2)) is None
            assert cache.resolve(conn, v2)["status"] == "deleted"
    finally:
        conn.execute("RESET ROLE")
        for version in (v1, v2, v3):
            cache.invalidate(version)
        cache.close()


def test_real_deferred_commit_failure_preserves_active_and_retries(tile_fixture):
    import psycopg

    conn, dataset, config = tile_fixture
    first, second = release(config, dataset), release(config, dataset)
    v1, v2 = UUID(first["tileVersion"]), UUID(second["tileVersion"])

    def invalidate_commit(step):
        if step == "before_commit":
            conn.execute(
                "DELETE FROM map_data.active_tile_versions WHERE source_system=%s",
                (config.source_system,),
            )

    with TileLock(conn, 3, "vector") as lease:
        publish_locked(config, lease, v1)
        with pytest.raises(psycopg.errors.RaiseException):
            publish_locked(config, lease, v2, fault=invalidate_commit)
        assert pointers(conn, config)["active_version"] == v1
        assert (
            conn.execute("SELECT status FROM map_data.tile_versions WHERE id=%s", (v2,)).fetchone()[
                "status"
            ]
            == "validated"
        )
        assert publish_locked(config, lease, v2)["published"]
        assert pointers(conn, config)["active_version"] == v2


def test_gc_rejects_replaced_release_directory_and_preserves_external_files(tile_fixture, tmp_path):
    import shutil

    conn, dataset, config = tile_fixture
    report = release(config, dataset)
    version = UUID(report["tileVersion"])
    conn.execute(
        "UPDATE map_data.tile_versions SET status='failed',cleanup_status='pending' WHERE id=%s",
        (version,),
    )
    folder = config.root / report["stagingPath"]
    shutil.rmtree(folder)
    external = tmp_path / "outside"
    external.mkdir()
    (external / "keep").write_text("safe")
    folder.symlink_to(external, target_is_directory=True)
    with TileLock(conn, 3, "vector") as lease:
        with pytest.raises(TileError) as error:
            cleanup_files(config, lease)
        assert error.value.code == "CLEANUP_PENDING"
        assert (external / "keep").read_text() == "safe"
        folder.unlink()
        assert cleanup_files(config, lease)["deleted"] == [str(version)]

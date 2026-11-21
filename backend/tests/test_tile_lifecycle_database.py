"""Lifecycle/permission/concurrency gates on disposable PostGIS and real Redis.

Synthetic DB publications test constraints only. Filesystem publication is
deliberately left to the later publish stage, after both full bake pipelines.
"""

import asyncio
import json
import os
import threading
import time
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from map_sync.grants import grant_tile_roles
from map_sync.locks import lock_dataset_lifecycle
from map_sync.migrate import migrate
from map_sync.sync import cleanup
from map_tiles.geometry import canon
from map_tiles.lifecycle import BuildHeartbeat, BuildSpec, TileError, TileLock, heartbeat
from map_tiles.status_cache import AsyncVersionStatusCache, VersionStatusCache, status_key


@pytest.fixture
def lifecycle_db():
    url = os.environ.get("TEST_MAP_DB_URL")
    if not url:
        pytest.skip("TEST_MAP_DB_URL required for disposable PostGIS")
    source = "tiles_test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        migrate(conn)
        datasets = []
        for index in range(9):
            run, dataset = uuid4(), uuid4()
            conn.execute(
                """INSERT INTO map_data.sync_runs
                (id,source_system,map_id,status,rules_version)
                VALUES (%s,%s,3,'succeeded','fixture')""",
                (run, source),
            )
            conn.execute(
                """INSERT INTO map_data.datasets
                (id,run_id,source_system,map_id,status,created_at)
                VALUES (%s,%s,%s,3,'building',clock_timestamp()-(%s*interval '1 hour'))""",
                (dataset, run, source, index),
            )
            conn.execute("UPDATE map_data.datasets SET status='ready' WHERE id=%s", (dataset,))
            datasets.append(dataset)
        profile, digest = canon(15)
        spec = BuildSpec(
            source, 3, "vector", datasets[-1], "fixture", "f" * 64, profile, digest, 1, 14, 18, {}
        )
        try:
            yield conn, url, datasets, spec
        finally:
            with conn.transaction():
                conn.execute(
                    "UPDATE map_data.tile_versions SET status='retired',cleanup_status='pending' WHERE source_system=%s AND status='ready'",
                    (source,),
                )
                conn.execute(
                    "DELETE FROM map_data.active_tile_versions WHERE source_system=%s", (source,)
                )
                conn.execute("DELETE FROM map_data.tile_versions WHERE source_system=%s", (source,))
                conn.execute("DELETE FROM map_data.tile_builds WHERE source_system=%s", (source,))
                conn.execute(
                    "DELETE FROM map_data.query_leases WHERE dataset_id=ANY(%s)", (datasets,)
                )
                conn.execute(
                    "DELETE FROM map_data.active_datasets WHERE source_system=%s", (source,)
                )
                conn.execute("DELETE FROM map_data.datasets WHERE source_system=%s", (source,))
                conn.execute("DELETE FROM map_data.sync_runs WHERE source_system=%s", (source,))


def publish_model(lease, build):
    """Only a schema gate: no final filesystem release is claimed here."""
    lease.validated(
        build, {"tileVersion": str(build.version), "datasetVersion": str(build.dataset)}
    )
    conn = lease.conn
    with conn.transaction():
        lock_dataset_lifecycle(conn)
        pointer = conn.execute(
            """SELECT * FROM map_data.active_tile_versions WHERE source_system=
            (SELECT source_system FROM map_data.tile_versions WHERE id=%s)
            AND map_id=%s AND format=%s FOR UPDATE""",
            (build.version, lease.map_id, lease.format),
        ).fetchone()
        if pointer and pointer["prev_version"]:
            conn.execute(
                "UPDATE map_data.tile_versions SET status='retired',cleanup_status='pending',retired_at=clock_timestamp() WHERE id=%s",
                (pointer["prev_version"],),
            )
        conn.execute(
            "UPDATE map_data.tile_versions SET status='ready',published_at=clock_timestamp() WHERE id=%s",
            (build.version,),
        )
        conn.execute(
            """INSERT INTO map_data.active_tile_versions
            (source_system,map_id,format,active_version,prev_version)
            SELECT source_system,map_id,format,id,%s FROM map_data.tile_versions WHERE id=%s
            ON CONFLICT (source_system,map_id,format) DO UPDATE
            SET active_version=EXCLUDED.active_version,prev_version=EXCLUDED.prev_version""",
            (pointer["active_version"] if pointer else None, build.version),
        )
    versions = [build.version]
    if pointer:
        versions.extend(v for v in (pointer["active_version"], pointer["prev_version"]) if v)
    lease.invalidate(versions)
    lease.finish(build)


def test_exclusive_locks_independent_formats_and_fencing(lifecycle_db):
    conn, url, _, spec = lifecycle_db
    with (
        psycopg.connect(url, autocommit=True, row_factory=dict_row) as other,
        TileLock(conn, 3, "vector") as vector,
    ):
        with pytest.raises(TileError, match="Another operation"), TileLock(other, 3, "vector"):
            pass
        with TileLock(other, 3, "raster") as raster:
            vector.check()
            raster.check()
        conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (vector.key,))
        with TileLock(other, 3, "vector"):
            with pytest.raises(TileError) as error:
                vector.start(spec)
            assert error.value.code == "BUILD_LOCK_LOST"


def test_explicit_dataset_and_frozen_release(lifecycle_db):
    conn, _, _, spec = lifecycle_db
    with TileLock(conn, 3, "vector") as lease:
        for wrong in (replace(spec, dataset=uuid4()), replace(spec, source_system="wrong")):
            with pytest.raises(TileError) as error:
                lease.start(wrong)
            assert error.value.code == "DATASET_UNAVAILABLE"
        build = lease.start(spec)
        assert heartbeat(conn, build.id, "baking")
        with pytest.raises(TileError):
            lease.finish(build)
        lease.validated(build, {"test": "validated"})
        lease.finish(build)
        assert not heartbeat(conn, build.id)
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute(
                "UPDATE map_data.tile_versions SET manifest='{}' WHERE id=%s", (build.version,)
            )
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute(
                "UPDATE map_data.tile_versions SET tolerance_hash='changed' WHERE id=%s",
                (build.version,),
            )
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("DELETE FROM map_data.datasets WHERE id=%s", (spec.dataset,))


def test_cleanup_protects_both_formats_builds_and_leases(lifecycle_db):
    conn, _, datasets, spec = lifecycle_db
    for format, indices in (("vector", (7, 6)), ("raster", (5, 4))):
        with TileLock(conn, 3, format) as lease:
            for index in indices:
                build = lease.start(
                    replace(
                        spec,
                        format=format,
                        dataset=datasets[index],
                        maxzoom=15 if format == "raster" else 14,
                    )
                )
                publish_model(lease, build)
    query = uuid4()
    conn.execute(
        """INSERT INTO map_data.query_leases (id,dataset_id,query,expires_at)
        VALUES (%s,%s,'{}',clock_timestamp()+interval '1 hour')""",
        (query, datasets[2]),
    )
    with TileLock(conn, 3, "vector") as lease:
        running = lease.start(replace(spec, dataset=datasets[3]))
        assert cleanup(conn, keep=2) == 1  # only unreferenced oldest index 8
        kept = {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM map_data.datasets WHERE source_system=%s", (spec.source_system,)
            )
        }
        assert kept == set(datasets[:8])
        lease.fail(running)
        assert cleanup(conn, keep=2) == 1  # failed worker releases index 3
        row = conn.execute(
            "SELECT dataset_id,dataset_version FROM map_data.tile_versions WHERE id=%s",
            (running.version,),
        ).fetchone()
        assert row["dataset_id"] is None and row["dataset_version"] == datasets[3]
    conn.execute(
        "UPDATE map_data.query_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
        (query,),
    )
    assert cleanup(conn, keep=2) == 1  # expired query releases index 2
    for format in ("vector", "raster"):
        count = conn.execute(
            "SELECT count(*) AS n FROM map_data.tile_versions WHERE source_system=%s AND format=%s AND status='ready'",
            (spec.source_system, format),
        ).fetchone()["n"]
        assert count == 2


def test_recovery_requires_actual_free_lock_and_stale_heartbeat(lifecycle_db):
    conn, url, _, spec = lifecycle_db
    former = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    lease = TileLock(former, 3, "vector").__enter__()
    build = lease.start(spec)
    former.execute(
        "UPDATE map_data.tile_builds SET heartbeat_at=clock_timestamp()-interval '10 minutes' WHERE id=%s",
        (build.id,),
    )
    with pytest.raises(TileError):
        TileLock(conn, 3, "vector").__enter__()
    former.close()  # authoritative lock release, not just a stale heartbeat
    heartbeat(conn, build.id)
    with TileLock(conn, 3, "vector") as next_lease:
        with pytest.raises(TileError) as error:
            next_lease.recover(dead_seconds=1)
        assert error.value.code == "BUILD_IN_PROGRESS"
        conn.execute(
            "UPDATE map_data.tile_builds SET heartbeat_at=clock_timestamp()-interval '10 minutes' WHERE id=%s",
            (build.id,),
        )
        assert next_lease.recover(dead_seconds=1) == [build.version]
        assert next_lease.recover(dead_seconds=1) == []
        with pytest.raises(TileError) as error:
            next_lease.start(spec)
        assert error.value.code == "CLEANUP_PENDING"
        with TileLock(conn, 3, "raster") as raster:
            other = raster.start(replace(spec, format="raster", maxzoom=15))
            raster.fail(other)


def test_constraints_reject_orphan_ready_and_bad_scope(lifecycle_db):
    conn, _, _, spec = lifecycle_db
    with TileLock(conn, 3, "vector") as lease:
        build = lease.start(spec)
        lease.validated(build, {"test": "model"})
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute(
                "UPDATE map_data.tile_versions SET status='ready' WHERE id=%s", (build.version,)
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                """INSERT INTO map_data.active_tile_versions VALUES
                (%s,3,'raster',%s,NULL,clock_timestamp())""",
                (spec.source_system, build.version),
            )
        assert (
            conn.execute(
                "SELECT status FROM map_data.tile_versions WHERE id=%s", (build.version,)
            ).fetchone()["status"]
            == "validated"
        )


def test_real_worker_and_api_grants(lifecycle_db):
    conn, _, _, spec = lifecycle_db
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='map_api'").fetchone():
        conn.execute("CREATE ROLE map_api NOLOGIN")
    grant_tile_roles(conn)
    with TileLock(conn, 3, "raster") as raster:
        raster_build = raster.start(replace(spec, format="raster", maxzoom=15))
    with TileLock(conn, 3, "raster") as raster:
        raster.fail(raster_build)
    conn.execute(
        "UPDATE map_data.tile_versions SET cleanup_status='done' WHERE id=%s",
        (raster_build.version,),
    )
    conn.execute("GRANT USAGE ON SCHEMA map_data TO map_api")
    try:
        conn.execute("SET ROLE map_tiles_vector")
        assert (
            conn.execute(
                "UPDATE map_data.tile_builds SET stage='foreign' WHERE id=%s RETURNING id",
                (raster_build.id,),
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "UPDATE map_data.tile_versions SET error_code='foreign' WHERE id=%s RETURNING id",
                (raster_build.version,),
            ).fetchone()
            is None
        )
        with TileLock(conn, 3, "vector") as lease:
            build = lease.start(spec)
            lease.validated(build, {"test": "privileges"})
            publish_model_from_validated(lease, build)
        with (
            TileLock(conn, 3, "raster") as raster,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            raster.start(replace(spec, format="raster", maxzoom=15))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM map_data.tile_versions WHERE id=%s", (build.version,))
        conn.execute("RESET ROLE")
        conn.execute("SET ROLE map_api")
        assert (
            conn.execute(
                "SELECT status FROM map_data.tile_versions WHERE id=%s", (build.version,)
            ).fetchone()["status"]
            == "ready"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE map_data.tile_versions SET status='retired',cleanup_status='pending' WHERE id=%s",
                (build.version,),
            )
    finally:
        conn.execute("RESET ROLE")


def publish_model_from_validated(lease, build):
    # Single initial version, same transaction invariants as publish_model.
    with lease.conn.transaction():
        lock_dataset_lifecycle(lease.conn)
        lease.conn.execute(
            "UPDATE map_data.tile_versions SET status='ready',published_at=clock_timestamp() WHERE id=%s",
            (build.version,),
        )
        lease.conn.execute(
            """INSERT INTO map_data.active_tile_versions
            (source_system,map_id,format,active_version)
            SELECT source_system,map_id,format,id FROM map_data.tile_versions WHERE id=%s""",
            (build.version,),
        )
    lease.invalidate([build.version])
    lease.finish(build)


def test_snapshot_pin_is_serialized_before_cleanup(lifecycle_db):
    conn, url, _, spec = lifecycle_db
    pinned, release, done = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def start():
        try:
            with (
                psycopg.connect(url, autocommit=True, row_factory=dict_row) as other,
                TileLock(other, 3, "vector") as lease,
                other.transaction(),
            ):
                lock_dataset_lifecycle(other)
                pinned.set()
                assert release.wait(5)
                lease.start(spec)
        except Exception as error:  # noqa: BLE001 -- assert captured thread failures below
            errors.append(error)

    def clean():
        try:
            with psycopg.connect(url, autocommit=True, row_factory=dict_row) as other:
                cleanup(other, keep=2)
        except Exception as error:  # noqa: BLE001 -- assert captured thread failures below
            errors.append(error)
        finally:
            done.set()

    a = threading.Thread(target=start)
    a.start()
    assert pinned.wait(5)
    b = threading.Thread(target=clean)
    b.start()
    deadline = time.monotonic() + 5
    blocked = False
    while time.monotonic() < deadline:
        if conn.execute(
            "SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event='advisory'"
        ).fetchone():
            blocked = True
            break
        time.sleep(0.01)
    release.set()
    a.join(5)
    b.join(5)
    assert blocked and done.is_set() and not errors
    assert conn.execute("SELECT 1 FROM map_data.datasets WHERE id=%s", (spec.dataset,)).fetchone()


@pytest.fixture
def redis_cache():
    url = os.environ.get("TEST_MAP_REDIS_URL")
    if not url:
        pytest.skip("TEST_MAP_REDIS_URL required for real Redis tests")
    cache = VersionStatusCache.from_url(url, ttl=1)
    assert cache.client.ping()
    yield cache, url
    cache.close()


def test_real_redis_miss_hit_invalidation_and_stale_fill_ttl(lifecycle_db, redis_cache):
    conn, _, _, spec = lifecycle_db
    cache, _ = redis_cache
    with TileLock(conn, 3, "vector", cache=cache) as lease:
        first = lease.start(spec)
        publish_model(lease, first)
        ready = cache.resolve(conn, first.version)
        assert ready["status"] == "ready"
        assert 0 < cache.client.ttl(status_key(first.version)) <= 1
        assert cache.resolve(None, first.version) == ready  # no DB access on hit
        assert cache.hits == 1
        second = lease.start(spec)
        publish_model(lease, second)
        assert cache.client.get(status_key(first.version)) is None
        third = lease.start(spec)
        publish_model(lease, third)
        assert cache.resolve(conn, first.version)["status"] == "retired"
        # Force the documented race: old DB read refills AFTER retire/invalidate.
        cache.client.set(status_key(first.version), json.dumps(ready), ex=cache.ttl)
        assert cache.resolve(conn, first.version)["status"] == "ready"
        assert cache.resolve(conn, first.version, force=True)["status"] == "retired"
        cache.client.set(status_key(first.version), json.dumps(ready), ex=cache.ttl)
        deadline = time.monotonic() + 2
        while (
            cache.client.get(status_key(first.version)) is not None and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert cache.resolve(conn, first.version)["status"] == "retired"
        assert cache.resolve(conn, uuid4()) is None


def test_sync_and_async_redis_outage_fall_back_to_db(lifecycle_db):
    conn, url, _, spec = lifecycle_db
    with TileLock(conn, 3, "vector") as lease:
        build = lease.start(spec)
    unavailable = "redis://127.0.0.1:1/0"
    cache = VersionStatusCache.from_url(unavailable)
    try:
        assert cache.resolve(conn, build.version)["status"] == "building"
        cache.invalidate(build.version)
        assert cache.errors >= 1
    finally:
        cache.close()

    async def verify():
        cache = AsyncVersionStatusCache.from_url(unavailable)
        try:
            async with await psycopg.AsyncConnection.connect(
                url, autocommit=True, row_factory=dict_row
            ) as other:
                assert (await cache.resolve(other, build.version))["status"] == "building"
                await cache.invalidate(build.version)
                assert cache.errors >= 1
        finally:
            await cache.close()

    asyncio.run(verify())


def test_async_real_redis_and_bad_cache_record(lifecycle_db, redis_cache):
    conn, url, _, spec = lifecycle_db
    sync, redis_url = redis_cache
    with TileLock(conn, 3, "vector") as lease:
        build = lease.start(spec)

    async def verify():
        cache = AsyncVersionStatusCache.from_url(redis_url, ttl=1)
        try:
            async with await psycopg.AsyncConnection.connect(
                url, autocommit=True, row_factory=dict_row
            ) as other:
                record = await cache.resolve(other, build.version)
                assert (await cache.resolve(None, build.version)) == record
                await cache.invalidate(build.version)
                assert sync.client.get(status_key(build.version)) is None
                sync.client.set(status_key(build.version), "null", ex=1)
                assert (await cache.resolve(other, build.version)) == record
                sync.client.set(status_key(build.version), "{}", ex=1)
                assert (await cache.resolve(other, build.version)) == record
        finally:
            await cache.close()

    asyncio.run(verify())


def test_background_heartbeat_detects_lost_session_and_stops(lifecycle_db):
    conn, url, _, spec = lifecycle_db

    def connect():
        return psycopg.connect(url, autocommit=True, row_factory=dict_row, connect_timeout=5)

    with TileLock(conn, 3, "vector") as lease:
        build = lease.start(spec)
        before = conn.execute(
            "SELECT heartbeat_at FROM map_data.tile_builds WHERE id=%s", (build.id,)
        ).fetchone()["heartbeat_at"]
        with (
            pytest.raises(TileError) as failure,
            BuildHeartbeat(lease, build, connect, interval=0.02) as monitor,
        ):
            assert (
                conn.execute(
                    "SELECT heartbeat_at FROM map_data.tile_builds WHERE id=%s", (build.id,)
                ).fetchone()["heartbeat_at"]
                > before
            )
            conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (lease.key,))
            deadline = time.monotonic() + 3
            while monitor.error is None and time.monotonic() < deadline:
                time.sleep(0.01)
            monitor.check()
        assert failure.value.code == "BUILD_HEARTBEAT_FAILED"
        assert not monitor.thread.is_alive()
        after = conn.execute(
            "SELECT heartbeat_at FROM map_data.tile_builds WHERE id=%s", (build.id,)
        ).fetchone()["heartbeat_at"]
        time.sleep(0.05)
        assert (
            conn.execute(
                "SELECT heartbeat_at FROM map_data.tile_builds WHERE id=%s", (build.id,)
            ).fetchone()["heartbeat_at"]
            == after
        )


def test_recover_preserves_already_published_release(lifecycle_db):
    conn, _, _, spec = lifecycle_db
    with TileLock(conn, 3, "vector") as lease:
        build = lease.start(spec)
        lease.validated(build, {"test": "ready_before_crash"})
        # Simulate the transaction commit, then death before build.finish().
        with conn.transaction():
            conn.execute(
                "UPDATE map_data.tile_versions SET status='ready',published_at=clock_timestamp() WHERE id=%s",
                (build.version,),
            )
            conn.execute(
                "INSERT INTO map_data.active_tile_versions (source_system,map_id,format,active_version) VALUES (%s,3,'vector',%s)",
                (spec.source_system, build.version),
            )
        conn.execute(
            "UPDATE map_data.tile_builds SET heartbeat_at=clock_timestamp()-interval '5 minutes' WHERE id=%s",
            (build.id,),
        )
    with TileLock(conn, 3, "vector") as lease:
        assert lease.recover() == [build.version]
        assert conn.execute(
            "SELECT status,stage FROM map_data.tile_builds WHERE id=%s", (build.id,)
        ).fetchone() == {"status": "succeeded", "stage": "published"}
        assert (
            conn.execute(
                "SELECT status FROM map_data.tile_versions WHERE id=%s", (build.version,)
            ).fetchone()["status"]
            == "ready"
        )
        assert (
            conn.execute(
                "SELECT active_version FROM map_data.active_tile_versions WHERE source_system=%s",
                (spec.source_system,),
            ).fetchone()["active_version"]
            == build.version
        )


def test_deleted_tombstone_survives_dataset_cleanup(lifecycle_db):
    conn, _, _, spec = lifecycle_db
    with TileLock(conn, 3, "vector") as lease:
        build = lease.start(spec)
        lease.fail(build)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE map_data.tile_versions SET status='deleted' WHERE id=%s", (build.version,)
            )
        conn.execute(
            "UPDATE map_data.tile_versions SET status='deleted',cleanup_status='done',deleted_at=clock_timestamp() WHERE id=%s",
            (build.version,),
        )
        assert cleanup(conn, keep=2) > 0
        tombstone = conn.execute(
            "SELECT dataset_id,dataset_version,status FROM map_data.tile_versions WHERE id=%s",
            (build.version,),
        ).fetchone()
        assert tombstone == {
            "dataset_id": None,
            "dataset_version": spec.dataset,
            "status": "deleted",
        }
        assert VersionStatusCache().resolve(conn, build.version)["status"] == "deleted"
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute(
                "UPDATE map_data.tile_versions SET status='building',cleanup_status='none',dataset_id=%s WHERE id=%s",
                (spec.dataset, build.version),
            )
        # Tombstone with completed cleanup permits the next build.
        lease.start(
            replace(
                spec,
                dataset=conn.execute(
                    "SELECT id FROM map_data.datasets WHERE source_system=%s ORDER BY created_at DESC LIMIT 1",
                    (spec.source_system,),
                ).fetchone()["id"],
            )
        )


def test_model_rollback_invalidate_restores_dataset_version(lifecycle_db, redis_cache):
    conn, _, datasets, spec = lifecycle_db
    cache, _ = redis_cache
    with TileLock(conn, 3, "vector", cache=cache) as lease:
        first = lease.start(spec)
        publish_model(lease, first)
        second = lease.start(replace(spec, dataset=datasets[-2]))
        publish_model(lease, second)
        assert cache.resolve(conn, second.version)["dataset_version"] == str(datasets[-2])
        assert cache.resolve(conn, first.version)["dataset_version"] == str(datasets[-1])
        # Schema model: exchange ready pointers atomically; production rollback
        # includes filesystem checks and is implemented at stage 6.
        with conn.transaction():
            lock_dataset_lifecycle(conn)
            conn.execute(
                "UPDATE map_data.active_tile_versions SET active_version=prev_version,prev_version=active_version WHERE source_system=%s AND map_id=3 AND format='vector'",
                (spec.source_system,),
            )
        lease.invalidate([first.version, second.version])
        assert cache.client.get(status_key(first.version)) is None
        assert cache.client.get(status_key(second.version)) is None
        active = conn.execute(
            "SELECT active_version FROM map_data.active_tile_versions WHERE source_system=%s",
            (spec.source_system,),
        ).fetchone()["active_version"]
        assert cache.resolve(conn, active)["dataset_version"] == str(datasets[-1])

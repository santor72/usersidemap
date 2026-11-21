"""Build lease, fencing and snapshot protection."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from threading import Event, Thread
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from map_sync.locks import lock_dataset_lifecycle


class TileError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

    def __reduce__(self):
        return type(self), (self.code, str(self))


@dataclass(frozen=True)
class BuildSpec:
    source_system: str
    map_id: int
    format: str
    dataset: UUID
    rules_version: str
    rules_hash: str
    geometry_profile: dict
    tolerance_hash: str
    minzoom: int
    maxzoom: int
    display_maxzoom: int
    parameters: dict


@dataclass(frozen=True)
class Build:
    id: UUID
    version: UUID
    dataset: UUID
    staging_path: str
    final_path: str


class TileLock(AbstractContextManager):
    """A dedicated session owns the lease throughout bake/publish/cleanup.

    Never pass a pooled connection that can be returned while this lease lives.
    Heartbeats use another connection; they cannot replace this exclusive lock.
    """

    def __init__(self, conn, map_id, format, *, cache=None):
        if type(map_id) is not int or map_id <= 0 or format not in ("vector", "raster"):
            raise ValueError("Invalid map/format")
        if not conn.autocommit:
            raise ValueError("Tile lease requires a dedicated autocommit connection")
        self.conn, self.map_id, self.format, self.cache = conn, map_id, format, cache
        self.key = f"map:tiles:{map_id}:{format}"
        self.held = False

    def __enter__(self):
        row = self.conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked", (self.key,)
        ).fetchone()
        if not row["locked"]:
            raise TileError("BUILD_LOCKED", "Another operation owns this map/format")
        self.held = True
        return self

    def __exit__(self, *_):
        if self.held and not self.conn.closed:
            try:
                self.conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (self.key,))
            except psycopg.Error:
                self.conn.close()
        self.held = False

    def check(self):
        if not self.held or self.conn.closed:
            raise TileError("BUILD_LOCK_LOST", "Build session lock is unavailable")
        # A closed/replaced backend cannot keep the lease alive via heartbeats.
        row = self.conn.execute(
            """SELECT EXISTS (SELECT 1 FROM pg_locks
            WHERE locktype='advisory' AND pid=pg_backend_pid() AND granted AND objsubid=1
            AND classid::bigint=((hashtextextended(%s,0)>>32)&4294967295)
            AND objid::bigint=(hashtextextended(%s,0)&4294967295)) AS held""",
            (self.key, self.key),
        ).fetchone()
        if not row["held"]:
            self.held = False
            raise TileError("BUILD_LOCK_LOST", "Build session no longer owns the exclusive lock")

    def invalidate(self, versions):
        if self.cache:
            for version in versions:
                self.cache.invalidate(version)

    def recover(self, dead_seconds=120):
        """Only the actual lock holder may recover stale, lockless former workers."""
        self.check()
        if dead_seconds <= 0:
            raise ValueError("Dead worker threshold must be positive")
        versions = []
        with self.conn.transaction():
            lock_dataset_lifecycle(self.conn)
            rows = self.conn.execute(
                """SELECT b.id,b.version_id,v.status AS version_status,
                b.heartbeat_at<clock_timestamp()-(%s*interval '1 second') AS stale
                FROM map_data.tile_builds b JOIN map_data.tile_versions v ON v.id=b.version_id
                WHERE b.map_id=%s AND b.format=%s AND b.status='running' FOR UPDATE OF b""",
                (dead_seconds, self.map_id, self.format),
            ).fetchall()
            if any(not row["stale"] for row in rows):
                raise TileError("BUILD_IN_PROGRESS", "Former worker heartbeat is still fresh")
            for row in rows:
                published = row["version_status"] == "ready"
                self.conn.execute(
                    """UPDATE map_data.tile_builds SET status=%s,
                    finished_at=clock_timestamp(),error_code=%s,
                    stage=CASE WHEN %s THEN 'published' ELSE stage END
                    WHERE id=%s""",
                    (
                        "succeeded" if published else "interrupted",
                        None if published else "WORKER_INTERRUPTED",
                        published,
                        row["id"],
                    ),
                )
                self.conn.execute(
                    """UPDATE map_data.tile_versions SET status='failed',
                    cleanup_status='pending',error_code='WORKER_INTERRUPTED'
                    WHERE id=%s AND status IN ('building','validated')""",
                    (row["version_id"],),
                )
                versions.append(row["version_id"])
        self.invalidate(versions)
        return versions

    def start(self, spec):
        self.check()
        if (spec.map_id, spec.format) != (self.map_id, self.format):
            raise ValueError("Build specification does not match the held lease")
        build_id, version = uuid4(), uuid4()
        staging = f"{spec.map_id}/{spec.format}/.staging/{version}"
        final = f"{spec.map_id}/{spec.format}/{version}"
        with self.conn.transaction():
            lock_dataset_lifecycle(self.conn)
            pinned = self.conn.execute(
                "SELECT map_data.tiles_pin_dataset(%s,%s,%s) AS id",
                (spec.source_system, spec.map_id, spec.dataset),
            ).fetchone()["id"]
            if pinned is None:
                raise TileError(
                    "DATASET_UNAVAILABLE",
                    "Explicit dataset must be ready and belong to this map/source",
                )
            pending = self.conn.execute(
                """SELECT 1 FROM map_data.tile_versions
                WHERE map_id=%s AND format=%s AND cleanup_status IN ('pending','error') LIMIT 1""",
                (self.map_id, self.format),
            ).fetchone()
            if pending:
                raise TileError(
                    "CLEANUP_PENDING", "Recover file cleanup before starting another build"
                )
            running = self.conn.execute(
                """SELECT 1 FROM map_data.tile_builds
                WHERE map_id=%s AND format=%s AND status='running' LIMIT 1""",
                (self.map_id, self.format),
            ).fetchone()
            if running:
                raise TileError("BUILD_IN_PROGRESS", "Recover the former build before starting")
            self.conn.execute(
                """INSERT INTO map_data.tile_builds
                (id,version_id,source_system,map_id,format,dataset_version,dataset_id,status,parameters)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'running',%s)""",
                (
                    build_id,
                    version,
                    spec.source_system,
                    spec.map_id,
                    spec.format,
                    spec.dataset,
                    spec.dataset,
                    Jsonb(spec.parameters),
                ),
            )
            self.conn.execute(
                """INSERT INTO map_data.tile_versions
                (id,build_id,source_system,map_id,format,dataset_version,dataset_id,status,
                 staging_path,final_path,minzoom,maxzoom,display_maxzoom,rules_version,rules_hash,
                 geometry_profile,tolerance_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'building',%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    version,
                    build_id,
                    spec.source_system,
                    spec.map_id,
                    spec.format,
                    spec.dataset,
                    spec.dataset,
                    staging,
                    final,
                    spec.minzoom,
                    spec.maxzoom,
                    spec.display_maxzoom,
                    spec.rules_version,
                    spec.rules_hash,
                    Jsonb(spec.geometry_profile),
                    spec.tolerance_hash,
                ),
            )
        return Build(build_id, version, spec.dataset, staging, final)

    def validated(self, build, manifest):
        self.check()
        with self.conn.transaction():
            row = self.conn.execute(
                """UPDATE map_data.tile_versions SET status='validated',
                validated_at=clock_timestamp(),manifest=%s WHERE id=%s AND status='building'
                AND map_id=%s AND format=%s RETURNING id""",
                (Jsonb(manifest), build.version, self.map_id, self.format),
            ).fetchone()
            if not row:
                raise TileError("BUILD_STATE_INVALID", "Version is not building")
            heartbeat(self.conn, build.id, "validated")

    def finish(self, build):
        self.check()
        row = self.conn.execute(
            """UPDATE map_data.tile_builds SET status='succeeded',
            finished_at=clock_timestamp() WHERE id=%s AND map_id=%s AND format=%s
            AND status='running' AND EXISTS (SELECT 1 FROM map_data.tile_versions v
                WHERE v.build_id=tile_builds.id AND v.status IN ('validated','ready')) RETURNING id""",
            (build.id, self.map_id, self.format),
        ).fetchone()
        if not row:
            raise TileError("BUILD_STATE_INVALID", "Build is not running")

    def fail(self, build, code="BUILD_FAILED"):
        self.check()
        with self.conn.transaction():
            row = self.conn.execute(
                """UPDATE map_data.tile_versions SET status='failed',
                cleanup_status='pending',error_code=%s WHERE id=%s AND map_id=%s AND format=%s
                AND status IN ('building','validated') RETURNING id""",
                (code, build.version, self.map_id, self.format),
            ).fetchone()
            if not row:
                raise TileError(
                    "BUILD_STATE_INVALID", "Cannot fail a published or terminal release"
                )
            self.conn.execute(
                """UPDATE map_data.tile_builds SET status='failed',
                finished_at=clock_timestamp(),error_code=%s WHERE id=%s AND status='running' """,
                (code, build.id),
            )
        self.invalidate([build.version])


def heartbeat(conn, build_id, stage=None):
    row = conn.execute(
        """UPDATE map_data.tile_builds SET heartbeat_at=clock_timestamp(),
        stage=COALESCE(%s,stage) WHERE id=%s AND status='running' RETURNING id""",
        (stage, build_id),
    ).fetchone()
    return row is not None


class BuildHeartbeat(AbstractContextManager):
    """A bounded heartbeat thread; callers check failure between worker batches.

    Exit this context before marking the build terminal. The separate connection
    verifies the main backend's advisory lock, not merely the Python held flag.
    """

    def __init__(self, lease, build, connect, *, interval=10):
        if interval <= 0:
            raise ValueError("Heartbeat interval must be positive")
        self.lease, self.build, self.connect, self.interval = lease, build, connect, interval
        self.stop, self.ready = Event(), Event()
        self.error = None
        self.thread = Thread(target=self.run, name=f"tile-heartbeat-{build.id}", daemon=True)

    def __enter__(self):
        self.lease.check()
        self.thread.start()
        if not self.ready.wait(15):
            self.stop.set()
            raise TileError("BUILD_HEARTBEAT_FAILED", "Heartbeat connection did not become ready")
        self.check()
        return self

    def run(self):
        try:
            with self.connect() as conn:
                if not conn.autocommit:
                    raise ValueError("Heartbeat requires an autocommit connection")
                conn.execute("SET statement_timeout=5000")
                while not self.stop.is_set():
                    row = conn.execute(
                        """SELECT EXISTS (SELECT 1 FROM pg_locks
                        WHERE locktype='advisory' AND pid=%s AND granted AND objsubid=1
                        AND classid::bigint=((hashtextextended(%s,0)>>32)&4294967295)
                        AND objid::bigint=(hashtextextended(%s,0)&4294967295)) AS held""",
                        (self.lease.conn.info.backend_pid, self.lease.key, self.lease.key),
                    ).fetchone()
                    if not row["held"] or not heartbeat(conn, self.build.id):
                        raise TileError(
                            "BUILD_LOCK_LOST", "Build heartbeat lost the lease or running record"
                        )
                    self.ready.set()
                    if self.stop.wait(self.interval):
                        break
        except Exception as error:  # noqa: BLE001 -- surface thread errors without connection secrets
            self.error = error
        finally:
            self.ready.set()

    def check(self):
        if self.error is not None:
            raise TileError(
                "BUILD_HEARTBEAT_FAILED", "Build heartbeat or session lock failed"
            ) from self.error

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(10)
        if self.thread.is_alive():
            raise TileError("BUILD_HEARTBEAT_FAILED", "Heartbeat did not stop within its deadline")
        # Preserve an exception already raised by the pipeline body.
        if not _ or _[0] is None:
            self.check()

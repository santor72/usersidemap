"""Fenced filesystem publication, retained pointers, rollback and recoverable GC."""

import json
import os
import shutil
from pathlib import Path

from map_sync.locks import lock_dataset_lifecycle

from .lifecycle import TileError, TileLock
from .manifest import validate_staging
from .status_cache import VersionStatusCache
from .storage import S3Path


def safe_path(root, relative):
    if isinstance(root, S3Path):
        return root / relative
    root = root.resolve()
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise TileError("TILE_PATH_UNSAFE", "Release path escaped tile root")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise TileError("TILE_PATH_UNSAFE", "Release path contains a symlink")
    if not current.resolve().is_relative_to(root):
        raise TileError("TILE_PATH_UNSAFE", "Release path escaped tile root")
    return current


def remove_tree(path):
    # rmtree never follows nested symlinks; safe_path rejects a replaced root.
    if isinstance(path, S3Path):
        path.remove()
    elif path.exists():
        shutil.rmtree(path)


def sync_directory(path):
    if isinstance(path, S3Path):
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def cleanup_files(config, lease, *, exclude=None, fault=lambda step: None):
    lease.check()
    # A validated final directory without ready is a rename-before-commit orphan.
    # Explicit retry publish excludes its UUID; general cleanup discards it.
    candidates = lease.conn.execute(
        """SELECT * FROM map_data.tile_versions
        WHERE map_id=%s AND format=%s AND status='validated' """,
        (lease.map_id, lease.format),
    ).fetchall()
    for row in candidates:
        if row["id"] == exclude:
            continue
        final = safe_path(config.root, row["final_path"])
        if final.exists():
            lease.conn.execute(
                """UPDATE map_data.tile_versions SET status='failed',
                cleanup_status='pending',error_code='UNPUBLISHED_FINAL' WHERE id=%s AND status='validated' """,
                (row["id"],),
            )
            lease.invalidate([row["id"]])
    rows = lease.conn.execute(
        """SELECT * FROM map_data.tile_versions
        WHERE map_id=%s AND format=%s AND cleanup_status IN ('pending','error')
        ORDER BY created_at""",
        (lease.map_id, lease.format),
    ).fetchall()
    cleaned, errors = [], []
    for row in rows:
        lease.check()
        try:
            # Never infer eligibility from a directory name or delete untracked files.
            retained = lease.conn.execute(
                """SELECT 1 FROM map_data.active_tile_versions
                WHERE active_version=%s OR prev_version=%s""",
                (row["id"], row["id"]),
            ).fetchone()
            if retained or row["status"] not in ("failed", "retired"):
                raise TileError("CLEANUP_STATE_INVALID", "Cannot remove a retained release")
            for field in ("staging_path", "final_path"):
                path = safe_path(config.root, row[field])
                fault("before_delete")
                remove_tree(path)
                if path.parent.exists():
                    sync_directory(path.parent)
            fault("after_delete")
            lease.check()
            lease.conn.execute(
                """UPDATE map_data.tile_versions SET status='deleted',
                cleanup_status='done',cleanup_error_code=NULL,deleted_at=clock_timestamp()
                WHERE id=%s AND status IN ('failed','retired')""",
                (row["id"],),
            )
            lease.invalidate([row["id"]])
            cleaned.append(str(row["id"]))
        except (OSError, TileError):
            lease.check()
            lease.conn.execute(
                """UPDATE map_data.tile_versions SET cleanup_status='error',
                cleanup_error_code='FILE_CLEANUP_FAILED' WHERE id=%s AND status IN ('failed','retired')""",
                (row["id"],),
            )
            errors.append(str(row["id"]))
    result = {"deleted": cleaned, "errors": errors, "cleanupPending": bool(errors)}
    if errors:
        error = TileError("CLEANUP_PENDING", "File cleanup failed; retry cleanup for this format")
        error.report = result
        raise error
    return result


def version_row(config, lease, version):
    row = lease.conn.execute(
        """SELECT * FROM map_data.tile_versions WHERE id=%s
        AND source_system=%s AND map_id=%s AND format=%s""",
        (version, config.source_system, lease.map_id, lease.format),
    ).fetchone()
    if not row:
        raise TileError("TILE_VERSION_UNKNOWN", "Release does not belong to this map/source/format")
    return row


def verify_release(config, lease, row, folder):
    if not folder.is_dir():
        raise TileError("TILE_FILE_MISSING", "Release directory is missing")
    identity = {
        "tileVersion": str(row["id"]),
        "datasetVersion": str(row["dataset_version"]),
        "mapId": row["map_id"],
        "sourceSystem": row["source_system"],
        "format": row["format"],
        "rulesVersion": row["rules_version"],
        "rulesHash": row["rules_hash"],
        "minzoom": row["minzoom"],
        "maxzoom": row["maxzoom"],
        "displayMaxzoom": row["display_maxzoom"],
        "geometryProfile": row["geometry_profile"],
        "toleranceHash": row["tolerance_hash"],
    }
    if any(row["manifest"].get(key) != value for key, value in identity.items()):
        raise TileError("MANIFEST_INVALID", "Manifest identity differs from release identity")
    if json.loads((folder / "manifest.json").read_text()) != row["manifest"]:
        raise TileError("MANIFEST_INVALID", "Disk manifest differs from immutable DB manifest")
    # Validate the full candidate before publication or rollback.
    validate_staging(folder, row["manifest"], lease.check)


def publish_locked(config, lease, version, *, fault=lambda step: None):
    lease.recover()
    cleanup_files(config, lease, exclude=version)
    row = version_row(config, lease, version)
    staging = safe_path(config.root, row["staging_path"])
    final = safe_path(config.root, row["final_path"])
    if row["status"] == "ready":
        pointer = lease.conn.execute(
            """SELECT active_version,prev_version FROM map_data.active_tile_versions
            WHERE source_system=%s AND map_id=%s AND format=%s""",
            (config.source_system, lease.map_id, lease.format),
        ).fetchone()
        if not pointer or pointer["active_version"] != version:
            raise TileError("PUBLISH_STATE_INVALID", "Ready previous release requires rollback")
        verify_release(config, lease, row, final)
        lease.invalidate([v for v in (version, pointer["prev_version"]) if v])
        return {
            "version": str(version),
            "datasetVersion": str(row["dataset_version"]),
            "published": True,
            "alreadyActive": True,
            "cleanup": cleanup_files(config, lease),
        }
    if row["status"] != "validated":
        raise TileError("PUBLISH_STATE_INVALID", "Only a validated release can publish")
    if staging.exists() and final.exists():
        raise TileError("PUBLISH_STATE_INVALID", "Both staging and final directories exist")
    folder = final if final.exists() else staging
    verify_release(config, lease, row, folder)
    lease.check()
    if folder == staging:
        fault("before_rename")
        # Atomic same-filesystem rename; never copy, overwrite or fill missing tiles.
        for path in () if isinstance(staging, S3Path) else staging.rglob("*"):
            if path.is_file():
                with path.open("rb") as file:
                    os.fsync(file.fileno())
        for path in sorted(
            (p for p in (() if isinstance(staging, S3Path) else staging.rglob("*")) if p.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        ):
            sync_directory(path)
        sync_directory(staging)
        staging.rename(final)
        sync_directory(staging.parent)
        sync_directory(final.parent)
        fault("after_rename")
    changed = [version]
    with lease.conn.transaction():
        lock_dataset_lifecycle(lease.conn)
        lease.check()
        current = lease.conn.execute(
            """SELECT * FROM map_data.active_tile_versions
            WHERE source_system=%s AND map_id=%s AND format=%s FOR UPDATE""",
            (config.source_system, lease.map_id, lease.format),
        ).fetchone()
        if current and current["prev_version"]:
            lease.conn.execute(
                """UPDATE map_data.tile_versions SET status='retired',
                cleanup_status='pending',retired_at=clock_timestamp() WHERE id=%s""",
                (current["prev_version"],),
            )
        lease.conn.execute(
            """UPDATE map_data.tile_versions SET status='ready',
            published_at=clock_timestamp() WHERE id=%s AND status='validated' """,
            (version,),
        )
        lease.conn.execute(
            """INSERT INTO map_data.active_tile_versions
            (source_system,map_id,format,active_version,prev_version)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT (source_system,map_id,format)
            DO UPDATE SET active_version=EXCLUDED.active_version,
            prev_version=EXCLUDED.prev_version,updated_at=clock_timestamp()""",
            (
                config.source_system,
                lease.map_id,
                lease.format,
                version,
                current["active_version"] if current else None,
            ),
        )
        if current:
            changed.extend(v for v in (current["active_version"], current["prev_version"]) if v)
        fault("before_commit")
    lease.invalidate(changed)
    fault("after_commit")
    result = {
        "version": str(version),
        "datasetVersion": str(row["dataset_version"]),
        "published": True,
        "alreadyActive": False,
    }
    try:
        result["cleanup"] = cleanup_files(config, lease, fault=fault)
    except TileError as error:
        if error.code != "CLEANUP_PENDING":
            raise
        result["cleanup"] = error.report
    return result


def rollback_locked(config, lease, *, target=None, fault=lambda step: None):
    lease.recover()
    try:
        cleanup_report = cleanup_files(config, lease)
    except TileError as error:
        if error.code != "CLEANUP_PENDING":
            raise
        cleanup_report = error.report
    pointer = lease.conn.execute(
        """SELECT * FROM map_data.active_tile_versions
        WHERE source_system=%s AND map_id=%s AND format=%s""",
        (config.source_system, lease.map_id, lease.format),
    ).fetchone()
    if pointer and target == pointer["active_version"]:
        row = version_row(config, lease, target)
        verify_release(config, lease, row, safe_path(config.root, row["final_path"]))
        lease.invalidate([v for v in (pointer["active_version"], pointer["prev_version"]) if v])
        return {
            "rolledBack": False,
            "alreadyActive": True,
            "version": str(target),
            "datasetVersion": str(row["dataset_version"]),
            "cleanup": cleanup_report,
        }
    if target is not None and pointer and target != pointer["prev_version"]:
        raise TileError("ROLLBACK_UNAVAILABLE", "Target must be the retained previous release")
    if not pointer or not pointer["prev_version"]:
        raise TileError("ROLLBACK_UNAVAILABLE", "No retained previous release")
    row = version_row(config, lease, pointer["prev_version"])
    if row["status"] != "ready":
        raise TileError("ROLLBACK_UNAVAILABLE", "Previous release is not ready")
    verify_release(config, lease, row, safe_path(config.root, row["final_path"]))
    with lease.conn.transaction():
        lock_dataset_lifecycle(lease.conn)
        lease.check()
        lease.conn.execute(
            """UPDATE map_data.active_tile_versions
            SET active_version=prev_version,prev_version=active_version,updated_at=clock_timestamp()
            WHERE source_system=%s AND map_id=%s AND format=%s""",
            (config.source_system, lease.map_id, lease.format),
        )
        fault("before_commit")
    lease.invalidate([pointer["active_version"], pointer["prev_version"]])
    return {
        "rolledBack": True,
        "cleanup": cleanup_report,
        "version": str(row["id"]),
        "datasetVersion": str(row["dataset_version"]),
    }


def operate(config, format, command, version=None):
    cache = (
        VersionStatusCache.from_url(config.redis_url) if config.redis_url else VersionStatusCache()
    )
    try:
        with config.connect() as conn, TileLock(conn, config.map_id, format, cache=cache) as lease:
            if command == "publish":
                return publish_locked(config, lease, version)
            if command == "rollback":
                return rollback_locked(config, lease, target=version)
            if command == "cleanup":
                lease.recover()
                return cleanup_files(config, lease)
            raise ValueError("Unknown tile lifecycle operation")
    finally:
        cache.close()

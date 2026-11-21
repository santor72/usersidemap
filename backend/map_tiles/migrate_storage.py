"""Copy retained filesystem releases to S3, checking every downloaded SHA256."""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from .config import TileConfig
from .lifecycle import TileError, TileLock
from .manifest import digest
from .publication import safe_path, verify_release
from .storage import MARKER, S3Path


def migrate(config, source_root, progress=print):
    if not isinstance(config.root, S3Path):
        raise TypeError("Migration target must be S3")
    format = os.environ.get("MAP_TILES_FORMAT", "vector")
    results = []
    with config.connect() as conn, TileLock(conn, config.map_id, format) as lease:
        rows = conn.execute(
            """SELECT v.* FROM map_data.tile_versions v
            JOIN map_data.active_tile_versions a ON v.id IN (a.active_version,a.prev_version)
            WHERE a.source_system=%s AND a.map_id=%s AND a.format=%s ORDER BY v.created_at""",
            (config.source_system, config.map_id, format),
        ).fetchall()
        for row in rows:
            local = safe_path(source_root, row["final_path"])
            verify_release(replace(config, root=source_root), lease, row, local)
            remote = config.root / row["staging_path"]
            if not (remote / MARKER).is_file():
                remote.mkdir()
            paths = [p for p in local.rglob("*") if p.is_file()]

            def copy(path, remote=remote, local=local):
                target = remote / path.relative_to(local).as_posix()
                payload = path.read_bytes()
                target.write_bytes(payload)
                if digest(target.read_bytes()) != digest(payload):
                    raise TileError("TILE_INVALID", "Migration download checksum mismatch")
                return len(payload)

            with ThreadPoolExecutor(max_workers=8) as pool:
                sizes = list(pool.map(copy, paths))
            if set(remote.inventory()) != {p.relative_to(local).as_posix() for p in paths}:
                raise TileError("MANIFEST_INVALID", "Migration object inventory mismatch")
            lease.check()
            remote.rename(config.root / row["final_path"])
            result = {
                "version": str(row["id"]),
                "format": format,
                "files": len(paths),
                "bytes": sum(sizes),
                "sha256Verified": len(paths),
            }
            results.append(result)
            progress(json.dumps(result))
    return results


def main():
    config = TileConfig.load(format=os.environ.get("MAP_TILES_FORMAT", "vector"))
    migrate(config, Path(sys.argv[1] if len(sys.argv) > 1 else "/tiles"))


if __name__ == "__main__":
    main()
